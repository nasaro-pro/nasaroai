from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ValidationError, model_validator
from playwright.async_api import Page

logger = logging.getLogger("arenax")

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

MAX_STEPS = 15
HISTORY_WINDOW = 5
MAX_ELEMENTS = 200
MAX_DOM_CHARS = 6000
ACTION_WAIT_MS = 800
LLM_MAX_TOKENS = 400
PARSE_FAIL_LIMIT = 3

AGENT_SYSTEM_PROMPT = """
너는 웹 브라우저를 조작하는 자율 에이전트다.

[출력 규칙 — 절대 위반 금지]
- 반드시 JSON 객체 하나만 출력한다.
- 스키마: {"action": "...", "target_id": "...", "value": "...", "reasoning": "..."}
- 마크다운 코드펜스(```), 설명 문장, 줄바꿈 추가 절대 금지.
- target_id와 value가 필요 없는 액션이면 null로 채워라.

[행동 규칙]
- 이전 action_history를 반드시 확인하고 똑같은 액션을 반복하지 마라.
- 에러가 났다면 scroll_down으로 다른 요소를 탐색하거나 navigate로 우회하라.
- 연속 3회 이상 같은 액션이면 다른 전략을 써라.
- 목표가 달성되었으면 즉시 action=done을 반환하고 reasoning에 결과를 서술하라.
- 확신이 없으면 scroll_down이나 wait으로 안전하게 상황을 보아라.

[액션 설명]
- click: target_id 요소 클릭
- type: target_id 요소에 value 입력 (기존 내용 지워짐)
- select: target_id 드롭다운에서 value 선택
- scroll_down / scroll_up: 페이지 스크롤
- press_key: 키보드 키 입력 (value = "Enter", "Tab", "Escape" 등)
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

    if (!el.dataset.agentId) {
      el.dataset.agentId = `f${frameIndex}-${counter++}`;
    } else {
      counter++;
    }

    const isVisible = (
      rect.top >= 0 && rect.bottom <= window.innerHeight &&
      rect.left >= 0 && rect.right <= window.innerWidth
    );

    items.push({
      id:          el.dataset.agentId,
      tag:         el.tagName.toLowerCase(),
      type:        el.type       || null,
      text:        (el.innerText || el.textContent || '').trim().slice(0, 80),
      placeholder: el.placeholder || null,
      href:        el.href        || null,
      value:       el.value       || null,
      checked:     el.type === 'checkbox' || el.type === 'radio'
                   ? el.checked : null,
      aria_label:  el.getAttribute('aria-label') || null,
      name:        el.name        || null,
      is_visible:  isVisible,
      rect: {
        top: Math.round(rect.top), left: Math.round(rect.left),
        width: Math.round(rect.width), height: Math.round(rect.height)
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
    reasoning: str

    @model_validator(mode="after")
    def check_required_fields(self) -> "AgentAction":
        needs_target = {"click", "type", "select"}
        needs_value = {"type", "select", "press_key", "navigate"}
        if self.action in needs_target and not self.target_id:
            self.action = "scroll_down"
            self.reasoning = f"[보정] {self.action} 액션에 target_id 없음 → scroll_down"
        if self.action in needs_value and not self.value:
            self.action = "scroll_down"
            self.reasoning = f"[보정] {self.action} 액션에 value 없음 → scroll_down"
        return self


def _extract_start_url(query: str) -> str:
    match = re.search(r"https?://\S+", query)
    if match:
        return match.group(0).rstrip(".,)")
    return f"https://duckduckgo.com/?q={urllib.parse.quote(query)}"


class CustomWebAgent:
    def __init__(self, openrouter_headers: dict[str, str], model: str) -> None:
        self.openrouter_headers = openrouter_headers
        self.model = model
        self._parse_fail_count = 0

    async def run(self, task: str, page: Page) -> str:
        action_history: list[dict[str, Any]] = []
        current_page = page
        self._parse_fail_count = 0

        for step in range(1, MAX_STEPS + 1):
            elements = await self._scan_dom(current_page)
            raw = await self._ask_llm(task, elements, action_history)
            action = self._safe_parse_action(raw)

            if action.action == "done":
                return action.reasoning

            current_page, error = await self._execute(current_page, action)

            action_history.append(
                {
                    "step": step,
                    "action": action.action,
                    "target": action.target_id,
                    "value": action.value,
                    "error": error,
                }
            )
            logger.info(
                "[Agent] step=%d action=%s target=%s error=%s reasoning=%s",
                step,
                action.action,
                action.target_id,
                error,
                action.reasoning[:80],
            )

            if self._should_stop(action_history):
                return "반복 또는 연속 실패가 감지되어 안전하게 종료했습니다."

        return "최대 단계 수에 도달했습니다. 목표를 완전히 달성하지 못했을 수 있습니다."

    async def _scan_dom(self, page: Page) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        for index, frame in enumerate(page.frames):
            try:
                items = await frame.evaluate(DOM_INJECTOR_JS, index)
                if items:
                    collected.extend(items)
            except Exception:
                continue
        ranked = self._rank_elements(collected)
        return ranked[:MAX_ELEMENTS]

    def _rank_elements(self, elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
        interactive_tags = {"button", "a", "input", "select", "textarea"}

        def score(item: dict[str, Any]) -> int:
            total = 0
            if item.get("is_visible"):
                total += 100
            if item.get("tag") in interactive_tags:
                total += 50
            if any(item.get(key) for key in ("text", "placeholder", "aria_label", "href")):
                total += 30
            top = (item.get("rect") or {}).get("top", 9999)
            if 0 <= top <= 600:
                total += 20
            return total

        return sorted(elements, key=score, reverse=True)

    async def _ask_llm(
        self,
        task: str,
        elements: list[dict[str, Any]],
        action_history: list[dict[str, Any]],
    ) -> str:
        elements_json = json.dumps(elements, ensure_ascii=False)[:MAX_DOM_CHARS]
        history_json = json.dumps(action_history[-HISTORY_WINDOW:], ensure_ascii=False)
        user_message = (
            f"목표: {task}\n\n"
            f"현재 페이지 요소 (JSON, 최대 {MAX_DOM_CHARS}자):\n"
            f"{elements_json}\n\n"
            f"이전 액션 히스토리 (최근 {HISTORY_WINDOW}개):\n"
            f"{history_json}"
        )
        payload = {
            "model": self.model,
            "max_tokens": LLM_MAX_TOKENS,
            "messages": [
                {"role": "system", "content": AGENT_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    OPENROUTER_CHAT_URL,
                    headers=self.openrouter_headers,
                    json=payload,
                )
            if response.status_code != 200:
                logger.warning("Agent LLM non-200 status=%s body=%s", response.status_code, response.text[:200])
                return ""
            data = response.json()
            return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        except Exception:
            logger.exception("Agent LLM request failed")
            return ""

    def _safe_parse_action(self, raw_text: str) -> AgentAction:
        parsed: dict[str, Any] | None = None
        candidates = [raw_text.strip()]
        stripped = raw_text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
            stripped = re.sub(r"\s*```$", "", stripped)
            candidates.append(stripped.strip())
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if match:
            candidates.append(match.group(0))

        for candidate in candidates:
            if not candidate:
                continue
            try:
                parsed = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue

        if parsed is None:
            self._parse_fail_count += 1
            if self._parse_fail_count >= PARSE_FAIL_LIMIT:
                return AgentAction(action="done", reasoning="연속 파싱 실패로 안전 종료")
            return AgentAction(action="scroll_down", reasoning="파싱 실패, 스크롤 재시도")

        try:
            action = AgentAction(**parsed)
            self._parse_fail_count = 0
            return action
        except ValidationError:
            self._parse_fail_count += 1
            if self._parse_fail_count >= PARSE_FAIL_LIMIT:
                return AgentAction(action="done", reasoning="연속 파싱 실패로 안전 종료")
            return AgentAction(action="scroll_down", reasoning="파싱 실패, 스크롤 재시도")

    async def _locator_for_target(self, page: Page, target_id: str):
        for frame in page.frames:
            locator = frame.locator(f'[data-agent-id="{target_id}"]')
            try:
                if await locator.count() > 0:
                    return locator
            except Exception:
                continue
        return page.locator(f'[data-agent-id="{target_id}"]')

    async def _execute(self, page: Page, action: AgentAction) -> tuple[Page, str | None]:
        current_page = page
        try:
            if action.action == "click":
                new_page_future = asyncio.ensure_future(
                    current_page.context.wait_for_event("page", timeout=3000)
                )
                try:
                    locator = await self._locator_for_target(current_page, action.target_id or "")
                    await locator.click(force=True, timeout=3000)
                    await asyncio.sleep(0.5)
                    if new_page_future.done() and not new_page_future.exception():
                        new_tab = new_page_future.result()
                        await new_tab.wait_for_load_state("domcontentloaded", timeout=8000)
                        current_page = new_tab
                except asyncio.TimeoutError:
                    pass
                except Exception as exc:
                    return current_page, f"click 실패: {exc}"
                finally:
                    new_page_future.cancel()

            elif action.action == "type":
                locator = await self._locator_for_target(current_page, action.target_id or "")
                await locator.fill(action.value or "", timeout=3000)

            elif action.action == "select":
                locator = await self._locator_for_target(current_page, action.target_id or "")
                await locator.select_option(action.value or "", timeout=3000)

            elif action.action == "scroll_down":
                await current_page.evaluate("window.scrollBy(0, 600)")

            elif action.action == "scroll_up":
                await current_page.evaluate("window.scrollBy(0, -600)")

            elif action.action == "press_key":
                await current_page.keyboard.press(action.value or "Enter")

            elif action.action == "navigate":
                await current_page.goto(
                    action.value or "",
                    wait_until="domcontentloaded",
                    timeout=15000,
                )

            elif action.action == "back":
                await current_page.go_back(wait_until="domcontentloaded", timeout=8000)

            elif action.action == "wait":
                ms = int(action.value or 1000)
                await asyncio.sleep(ms / 1000)

            elif action.action == "done":
                return current_page, None

            if action.action != "done":
                await current_page.wait_for_timeout(ACTION_WAIT_MS)

            return current_page, None
        except Exception as exc:
            return current_page, str(exc)

    def _should_stop(self, history: list[dict[str, Any]]) -> bool:
        recent = history[-4:]
        if len(recent) >= 4:
            actions = [item.get("action") for item in recent]
            if len(set(actions)) == 1:
                return True
            errors = [item.get("error") for item in recent]
            if all(error is not None for error in errors):
                return True
        return False
