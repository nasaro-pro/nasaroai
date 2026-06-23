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

logger = logging.getLogger("arenax")

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
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.warning("[Agent] LLM 호출 실패: %s", exc)
            return ""

    def _safe_parse_action(self, raw_text: str) -> AgentAction:
        fallback_scroll = AgentAction(action="scroll_down", reasoning="파싱 실패, 스크롤 재시도")
        fallback_done = AgentAction(action="done", reasoning="연속 파싱 실패로 안전 종료")

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
