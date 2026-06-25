from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
from typing import Literal

import httpx
from playwright.async_api import Page
from pydantic import BaseModel, model_validator

logger = logging.getLogger("nasaroai")

MAX_STEPS = 15
HISTORY_WINDOW = 5
MAX_ELEMENTS = 200
MAX_DOM_CHARS = 6000
ACTION_WAIT_MS = 800
LLM_MAX_TOKENS = 400
PARSE_FAIL_LIMIT = 3

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

KEY_ALIASES: dict[str, str] = {
    "enter": "Enter",
    "return": "Enter",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "space": "Space",
    "backspace": "Backspace",
    "arrowup": "ArrowUp",
    "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
}

AGENT_SYSTEM_PROMPT = """
너는 웹 브라우저를 조작하는 자율 에이전트다.

[출력 규칙 — 절대 위반 금지]
- 반드시 JSON 객체 하나만 출력한다.
- 스키마: {"action": "...", "target_id": "...", "value": "...", "reasoning": "..."}
- 마크다운 코드펜스, 설명 문장 절대 금지. 순수 JSON만 출력.
- 필요 없는 필드는 null로 채워라.

[행동 규칙]
- 이전 action_history를 보고 똑같은 액션을 반복하지 마라.
- 에러가 났다면 scroll_down으로 다른 요소를 탐색하거나 navigate로 우회하라.
- 연속 3회 동일 액션이면 반드시 다른 전략을 써라.
- 목표 달성 시 즉시 action=done, reasoning에 결과를 한국어로 서술하라.
- 확신이 없으면 scroll_down이나 wait으로 상황을 보아라.

[액션 목록]
- click: target_id 요소 클릭
- type: target_id 요소에 value 입력 (기존 내용 지워짐)
- select: target_id 드롭다운에서 value 선택 (option의 value 속성 우선, 없으면 텍스트)
- scroll_down / scroll_up: 페이지 스크롤
- press_key: 키보드 입력 (value = "Enter", "Tab", "Escape" 등)
- navigate: value URL로 이동
- back: 브라우저 뒤로가기
- wait: value ms 대기 (기본 1000)
- done: 작업 종료. reasoning에 최종 결과 서술.
""".strip()

DOM_INJECTOR_JS = """
(frameIndex) => {
  const TAGS = [
    'a','button','input','select','textarea',
    '[role="button"]','[role="link"]','[role="menuitem"]',
    '[role="option"]','[role="tab"]','[onclick]'
  ];
  const nodes = Array.from(document.querySelectorAll(TAGS.join(',')));
  let counter = 0;
  const items = [];
  for (const el of nodes) {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;

    if (!el.getAttribute('data-agent-id')) {
      el.setAttribute('data-agent-id', `f${frameIndex}-${counter}`);
    }
    counter++;

    const isVisible = (
      rect.top >= 0 && rect.bottom <= window.innerHeight &&
      rect.left >= 0 && rect.right <= window.innerWidth
    );
    items.push({
      id:          el.getAttribute('data-agent-id'),
      tag:         el.tagName.toLowerCase(),
      type:        el.type        || null,
      text:        (el.innerText || el.textContent || '').trim().slice(0, 80),
      placeholder: el.placeholder || null,
      href:        el.href         || null,
      value:       el.value        || null,
      checked:     (el.type === 'checkbox' || el.type === 'radio') ? el.checked : null,
      aria_label:  el.getAttribute('aria-label') || null,
      name:        el.name         || null,
      is_visible:  isVisible,
      rect: {
        top:    Math.round(rect.top),
        left:   Math.round(rect.left),
        width:  Math.round(rect.width),
        height: Math.round(rect.height)
      }
    });
  }
  return items;
}
"""


class AgentAction(BaseModel):
    action: Literal[
        "click",
        "type",
        "select",
        "scroll_down",
        "scroll_up",
        "press_key",
        "navigate",
        "back",
        "wait",
        "done",
    ]
    target_id: str | None = None
    value: str | None = None
    reasoning: str = ""

    @model_validator(mode="after")
    def validate_and_fix(self) -> "AgentAction":
        needs_target = {"click", "type", "select"}
        needs_value = {"type", "select", "press_key", "navigate"}

        if self.action in needs_target and not self.target_id:
            self.action = "scroll_down"
            self.reasoning = "[보정] target_id 없음 → scroll_down"
            return self

        if self.action in needs_value and not self.value:
            self.action = "scroll_down"
            self.reasoning = "[보정] value 없음 → scroll_down"
            return self

        if self.target_id and len(self.target_id) > 50:
            self.action = "scroll_down"
            self.reasoning = "[보정] target_id 형식 이상 → scroll_down"
            return self

        if self.value and len(self.value) > 2000:
            self.value = self.value[:2000]

        if self.action == "press_key" and self.value:
            self.value = KEY_ALIASES.get(self.value.lower(), self.value)

        if not self.reasoning:
            self.reasoning = "(reasoning 없음)"

        return self


def _extract_start_url(query: str) -> str:
    """쿼리에서 URL 추출. 없으면 DuckDuckGo 검색."""
    match = re.search(r"https?://\S+", query)
    if match:
        return match.group(0)
    return f"https://duckduckgo.com/?q={urllib.parse.quote(query)}"


class CustomWebAgent:
    def __init__(self, openrouter_headers: dict, model: str) -> None:
        self.headers = openrouter_headers
        self.model = model
        self._parse_fail_count = 0
        self._last_llm_error: str | None = None

    async def run(self, task: str, page: Page) -> str:
        action_history: list = []
        current_page = page
        self._parse_fail_count = 0

        for step in range(1, MAX_STEPS + 1):
            elements = await self._scan_dom(current_page)
            raw = await self._ask_llm(task, elements, action_history)
            action = self._safe_parse_action(raw)

            if action.action == "done":
                return action.reasoning or "작업이 완료되었습니다."

            current_page, error = await self._execute(current_page, action)

            logger.info(
                "[Agent] step=%d action=%s target=%s error=%s | %s",
                step,
                action.action,
                action.target_id,
                error,
                action.reasoning[:60],
            )

            action_history.append(
                {
                    "step": step,
                    "action": action.action,
                    "target": action.target_id,
                    "value": action.value,
                    "error": error,
                }
            )

            if self._should_stop(action_history):
                return "반복 또는 연속 실패가 감지되어 안전하게 종료했습니다."

        return "최대 단계 수에 도달했습니다. 목표를 완전히 달성하지 못했을 수 있습니다."

    async def _scan_dom(self, page: Page) -> list:
        all_elements: list = []
        for i, frame in enumerate(page.frames):
            try:
                items = await frame.evaluate(DOM_INJECTOR_JS, i)
                if isinstance(items, list):
                    all_elements.extend(items)
            except Exception:
                continue
        return self._rank_elements(all_elements)

    def _rank_elements(self, elements: list) -> list:
        priority_tags = {"button", "a", "input", "select", "textarea"}

        def score(el: dict) -> int:
            s = 0
            if el.get("is_visible"):
                s += 100
            if el.get("tag") in priority_tags:
                s += 50
            if any(el.get(field) for field in ("text", "placeholder", "aria_label", "href")):
                s += 30
            top = (el.get("rect") or {}).get("top", 9999)
            if 0 <= top <= 600:
                s += 20
            return s

        ranked = sorted(enumerate(elements), key=lambda item: score(item[1]), reverse=True)
        return [element for _, element in ranked][:MAX_ELEMENTS]

    async def _ask_llm(self, task: str, elements: list, action_history: list) -> str:
        elements_json = json.dumps(elements, ensure_ascii=False)[:MAX_DOM_CHARS]
        recent_history = action_history[-HISTORY_WINDOW:]
        user_msg = (
            f"목표: {task}\n\n"
            f"현재 페이지 요소 (JSON):\n{elements_json}\n\n"
            f"이전 액션 히스토리 (최근 {HISTORY_WINDOW}개):\n"
            f"{json.dumps(recent_history, ensure_ascii=False)}"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    OPENROUTER_CHAT_URL,
                    headers=self.headers,
                    json={
                        "model": self.model,
                        "max_tokens": LLM_MAX_TOKENS,
                        "stream": False,
                        "messages": [
                            {"role": "system", "content": AGENT_SYSTEM_PROMPT},
                            {"role": "user", "content": user_msg},
                        ],
                    },
                )

            try:
                data = resp.json()
            except Exception:
                data = {}

            # HTTP 에러 또는 응답 본문에 error 키가 있으면 원인을 명확히 남긴다.
            if resp.status_code >= 400 or (isinstance(data, dict) and data.get("error")):
                detail = ""
                if isinstance(data, dict) and data.get("error"):
                    error_obj = data["error"]
                    detail = error_obj.get("message") if isinstance(error_obj, dict) else str(error_obj)
                if not detail:
                    detail = resp.text[:300]
                logger.error("[Agent] LLM 호출 실패 status=%s detail=%s", resp.status_code, detail)
                self._last_llm_error = f"LLM 호출 실패 (status={resp.status_code}): {detail}"
                return ""

            choices = data.get("choices") if isinstance(data, dict) else None
            if not choices:
                logger.error("[Agent] LLM 응답에 choices 없음 raw=%s", resp.text[:300])
                self._last_llm_error = "LLM 응답에 choices가 없습니다."
                return ""

            content = (choices[0].get("message") or {}).get("content") or ""
            if not content.strip():
                logger.error("[Agent] LLM 빈 응답 raw=%s", resp.text[:300])
                self._last_llm_error = "LLM이 빈 응답을 반환했습니다."
                return ""

            self._last_llm_error = None
            return content
        except httpx.TimeoutException:
            logger.error("[Agent] LLM 호출 타임아웃 (30초 초과)")
            self._last_llm_error = "LLM 호출이 30초 내에 응답하지 않았습니다 (타임아웃)."
            return ""
        except Exception as exc:
            logger.error("[Agent] LLM 호출 중 예외: %s", exc, exc_info=True)
            self._last_llm_error = f"LLM 호출 중 예외: {exc}"
            return ""

    def _safe_parse_action(self, raw_text: str) -> AgentAction:
        fallback_scroll = AgentAction(action="scroll_down", reasoning="파싱 실패, 스크롤 재시도")
        done_reason = "연속 파싱 실패로 안전 종료"
        if self._last_llm_error:
            done_reason = f"연속 파싱 실패로 안전 종료 (원인: {self._last_llm_error})"
        fallback_done = AgentAction(action="done", reasoning=done_reason)

        # 빈 응답은 파싱 문제가 아니라 LLM 호출 실패이므로 곧바로 처리한다.
        if not raw_text:
            self._parse_fail_count += 1
            logger.warning(
                "[Agent] LLM 빈 응답 (호출 실패) %d회 원인=%s",
                self._parse_fail_count,
                self._last_llm_error,
            )
            if self._parse_fail_count >= PARSE_FAIL_LIMIT:
                return fallback_done
            return fallback_scroll

        def try_parse(text: str) -> AgentAction | None:
            try:
                parsed = json.loads(text)
                allowed = {
                    "click",
                    "type",
                    "select",
                    "scroll_down",
                    "scroll_up",
                    "press_key",
                    "navigate",
                    "back",
                    "wait",
                    "done",
                }
                if parsed.get("action") not in allowed:
                    return None
                return AgentAction(**parsed)
            except Exception:
                return None

        result = try_parse(raw_text)
        if result:
            self._parse_fail_count = 0
            return result

        cleaned = re.sub(r"```(?:json)?", "", raw_text).strip()
        result = try_parse(cleaned)
        if result:
            self._parse_fail_count = 0
            return result

        match = re.search(r"\{.*?\}", raw_text, re.DOTALL)
        if match:
            result = try_parse(match.group(0))
            if result:
                self._parse_fail_count = 0
                return result

        self._parse_fail_count += 1
        logger.warning("[Agent] 파싱 실패 %d회 raw=%s", self._parse_fail_count, raw_text[:80])
        if self._parse_fail_count >= PARSE_FAIL_LIMIT:
            return fallback_done
        return fallback_scroll

    async def _execute(self, current_page: Page, action: AgentAction) -> tuple[Page, str | None]:
        try:
            if action.action == "click":
                locator = current_page.locator(f'[data-agent-id="{action.target_id}"]').first
                try:
                    await locator.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                new_page_future = asyncio.ensure_future(
                    current_page.context.wait_for_event("page", timeout=3000)
                )
                try:
                    # Prefer a real (non-forced) click so site handlers fire correctly;
                    # fall back to a forced click if the element is obscured.
                    try:
                        await locator.click(timeout=4000)
                    except Exception:
                        await locator.click(force=True, timeout=3000)
                    await asyncio.sleep(0.5)
                    if new_page_future.done() and not new_page_future.exception():
                        new_tab = new_page_future.result()
                        await new_tab.wait_for_load_state("domcontentloaded", timeout=8000)
                        current_page = new_tab
                except asyncio.TimeoutError:
                    pass
                finally:
                    new_page_future.cancel()

            elif action.action == "type":
                locator = current_page.locator(f'[data-agent-id="{action.target_id}"]').first
                try:
                    await locator.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                await locator.click(timeout=3000)
                await locator.fill("")
                # Use real keystrokes so search boxes with key listeners react.
                await locator.type(action.value, delay=15)  # type: ignore[arg-type]

            elif action.action == "select":
                locator = current_page.locator(f'[data-agent-id="{action.target_id}"]').first
                try:
                    await locator.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                try:
                    await locator.select_option(value=action.value, timeout=3000)
                except Exception:
                    await locator.select_option(label=action.value, timeout=3000)  # type: ignore[arg-type]

            elif action.action == "scroll_down":
                await current_page.evaluate("window.scrollBy(0, 600)")

            elif action.action == "scroll_up":
                await current_page.evaluate("window.scrollBy(0, -600)")

            elif action.action == "press_key":
                await current_page.keyboard.press(action.value)  # type: ignore[arg-type]

            elif action.action == "navigate":
                await current_page.goto(
                    action.value,
                    wait_until="domcontentloaded",
                    timeout=15000,  # type: ignore[arg-type]
                )

            elif action.action == "back":
                await current_page.go_back(wait_until="domcontentloaded", timeout=8000)

            elif action.action == "wait":
                ms = int(action.value or 1000)
                await asyncio.sleep(ms / 1000)

            elif action.action == "done":
                return current_page, None

            await current_page.wait_for_timeout(ACTION_WAIT_MS)
            return current_page, None

        except Exception as exc:
            return current_page, str(exc)

    def _should_stop(self, history: list) -> bool:
        if len(history) < 4:
            return False
        recent = history[-4:]
        if len({item["action"] for item in recent}) == 1:
            return True
        if all(item["error"] is not None for item in recent):
            return True
        return False


# ---------------------------------------------------------------------------
# 스테이트리스 두뇌 (/agent/step 전용)
# ---------------------------------------------------------------------------
# 이 영역은 서버가 브라우저를 띄우지 않는 새 라인을 위한 코드다.
# 위의 CustomWebAgent(Playwright 기반, /agent/task)는 절대 건드리지 않는다.
#
# 설계 결정: CustomWebAgent 클래스에 메서드를 추가하지 않고, 모듈 수준 함수로
# 분리한다. 이유 ─
#   1) /agent/step은 "요청 1번 = 판단 1번"인 무상태(stateless) 호출이라
#      Page/_execute/_scan_dom 같은 인스턴스 수명 상태가 전혀 필요 없다.
#   2) 기존 클래스를 수정하면 /agent/task 경로에 회귀 위험이 생긴다. 함수로
#      두면 기존 클래스는 그대로 두고, _ask_llm()/_safe_parse_action()만
#      "조합(composition)"으로 재사용해 위험을 0으로 만든다.
#   3) 두 메서드는 이미 Page에 의존하지 않으므로 임시 인스턴스로 그대로 쓸 수 있다.
#
# [보안 메모]
#   - 비밀번호/카드번호 같은 민감 입력값은 애초에 서버로 들어오면 안 된다.
#     확장(background.js)의 스캔이 input[type="password"]의 value를 수집하지
#     않고 has_password 플래그만 보내도록 구현되어 있다(2025 기준 구현됨).
#     서버는 만약을 대비해 아래 detect_handoff에서 비밀번호 필드를 만나면
#     LLM 호출 자체를 하지 않고 즉시 사용자 핸드오프로 빠진다.
#   - 최종 제출(결제 확정, 신청 확정) 액션은 향후 "사용자 확인 게이트"를 거치도록
#     만들 계획이다(이번 단계 범위 아님). 현재는 결제 폼 감지 시 핸드오프로 정지한다.

CAPTCHA_KEYWORDS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "로봇이 아닙",
    "i'm not a robot",
    "im not a robot",
    "자동 가입 방지",
)

PAYMENT_KEYWORDS = (
    "card number",
    "cardnumber",
    "card-number",
    "카드번호",
    "카드 번호",
    "cvc",
    "cvv",
    "유효기간",
    "expiry",
    "결제하기",
    "결제 진행",
)


# 되돌리기 어려운 "최종 제출/결제성" 동작. 이런 클릭은 실행 전에 사용자 확인을 받는다.
CONFIRM_KEYWORDS = (
    "결제",
    "결제하기",
    "구매",
    "주문",
    "제출",
    "신청하기",
    "신청 완료",
    "확정",
    "동의하고",
    "submit",
    "buy",
    "purchase",
    "place order",
    "checkout",
    "confirm",
    "pay now",
)


def _element_by_id(elements: list, target_id: str | None) -> dict | None:
    if not target_id or not isinstance(elements, list):
        return None
    for el in elements:
        if isinstance(el, dict) and el.get("id") == target_id:
            return el
    return None


def needs_confirmation(action: "AgentAction", elements: list) -> tuple[bool, str]:
    """제출/결제처럼 되돌리기 어려운 클릭만 사용자 확인 대상으로 잡는다.

    과도한 확인은 사용성을 해치므로, 클릭 액션이면서 대상 요소의 라벨이
    결제/제출/구매 등 강한 키워드를 포함할 때만 True. (임계값은 조정 가능)
    """
    if action.action != "click":
        return False, ""
    el = _element_by_id(elements, action.target_id)
    if not el:
        return False, ""
    blob = " ".join(
        str(el.get(key) or "") for key in ("text", "value", "aria_label", "name")
    ).lower()
    for keyword in CONFIRM_KEYWORDS:
        if keyword.lower() in blob:
            label = (el.get("text") or el.get("value") or el.get("aria_label") or "").strip()[:40]
            return True, f"'{label}' 동작은 제출/결제처럼 되돌리기 어려울 수 있습니다."
    return False, ""


def detect_handoff(elements: list) -> tuple[bool, str]:
    """로그인/캡차/결제처럼 사용자가 직접 처리해야 하는 화면을 감지한다.

    감지되면 (True, 사유) 를 반환하고, 호출부는 LLM을 호출하지 않고 즉시
    handoff_required 응답을 돌려준다. 과하게 잡으면 작업이 자주 멈추므로,
    '단순 로그인 링크'가 아니라 '실제 입력 필드/캡차 위젯'이 화면에 있을 때만
    잡도록 보수적으로 설계했다(임계값은 운영하며 조정 필요).
    """
    if not isinstance(elements, list):
        return False, ""

    has_password = False
    has_captcha = False
    has_payment = False

    for el in elements:
        if not isinstance(el, dict):
            continue
        el_type = (el.get("type") or "").lower()
        if el.get("has_password") or el_type == "password":
            has_password = True
        blob = " ".join(
            str(el.get(key) or "")
            for key in ("text", "placeholder", "aria_label", "name", "id", "href")
        ).lower()
        if any(keyword in blob for keyword in CAPTCHA_KEYWORDS):
            has_captcha = True
        if any(keyword in blob for keyword in PAYMENT_KEYWORDS):
            has_payment = True

    if has_captcha:
        return True, "캡차(로봇 확인)가 감지되었습니다. 보안을 위해 이 단계는 사용자가 직접 처리해주세요."
    if has_password:
        return True, "로그인(비밀번호 입력) 화면이 감지되었습니다. 보안을 위해 로그인은 사용자가 직접 진행해주세요."
    if has_payment:
        return True, "결제 정보 입력 화면이 감지되었습니다. 안전을 위해 결제는 사용자가 직접 확인/진행해주세요."
    return False, ""


async def decide_next_step(
    headers: dict,
    models: list[str],
    task: str,
    elements: list,
    action_history: list,
    current_url: str = "",
) -> dict:
    """무상태 한 스텝 판단. 이미 elements가 주어지므로 Page/_scan_dom/_execute는
    쓰지 않고, 기존 _ask_llm()/_safe_parse_action()만 재사용한다.

    models: 우선순위 순서의 모델 목록. 앞 모델이 429/타임아웃 등으로 빈 응답을
    내면(주로 무료 모델 한도 초과) 다음 모델로 폴백한다. 모두 실패하면 마지막
    오류 사유를 담아 done으로 안전 종료한다."""
    handoff, reason = detect_handoff(elements)
    if handoff:
        return {
            "action": "done",
            "target_id": None,
            "value": None,
            "reasoning": reason,
            "done": True,
            "handoff_required": True,
            "confirm_required": False,
            "confirm_message": "",
        }

    candidates = [m for m in (models or []) if m] or ["openai/gpt-4o"]
    last_error = "알 수 없는 오류"

    for model in candidates:
        agent = CustomWebAgent(openrouter_headers=headers, model=model)
        raw = await agent._ask_llm(task, elements, action_history)
        if not raw:
            # 이 모델은 호출 실패(한도/타임아웃 등). 다음 후보로 폴백.
            last_error = agent._last_llm_error or last_error
            continue

        action = agent._safe_parse_action(raw)
        confirm_required, confirm_message = needs_confirmation(action, elements)
        return {
            "action": action.action,
            "target_id": action.target_id,
            "value": action.value,
            "reasoning": action.reasoning,
            "done": action.action == "done",
            "handoff_required": False,
            "confirm_required": confirm_required,
            "confirm_message": confirm_message,
        }

    return {
        "action": "done",
        "target_id": None,
        "value": None,
        "reasoning": f"무료 AI 모델 호출에 모두 실패했습니다. 잠시 후 다시 시도해주세요. (원인: {last_error})",
        "done": True,
        "handoff_required": False,
        "confirm_required": False,
        "confirm_message": "",
    }
