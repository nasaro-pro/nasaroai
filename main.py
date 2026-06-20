"""
ArenaX v2 Backend
-----------------
5개 AI 회사 라벨(OpenAI/Anthropic/Google/xAI/Perplexity)에 대해
OpenRouter의 무료(:free) 모델로 비교/토론 응답을 생성하는 FastAPI 서버.

설계 노트
~~~~~~~~~
OpenRouter 무료 카탈로그에는 회사별로 실제 그 회사가 만든 무료 모델이
있는 경우(OpenAI, Google, xAI)와 없는 경우(Anthropic, Perplexity)가
섞여 있다. UI 라벨은 그대로 두되, 실제 호출 모델은 MODEL_MAPPING에서
관리하고, 어떤 라벨이 실제 회사 모델인지는 IS_REAL_COMPANY_MODEL로
프론트에 알려준다.

무료 모델은 자주 만료/교체되고 레이트리밋(429)도 걸리므로, 한 라벨이
실패하면 정해진 횟수만큼 다른 후보 모델로 교체해서 재시도한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from collections.abc import Callable
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("arenax")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="ArenaX v2 Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# OpenRouter 설정
# ============================================================

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "HTTP-Referer": "https://arenax.com",
    "X-Title": "ArenaX",
    "Content-Type": "application/json",
}

REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
RATE_LIMIT_WAIT_SECONDS = 3.0
RATE_LIMIT_WAIT_CAP_SECONDS = 8.0
MAX_DEBATE_LABEL_RETRIES = 3

logger.info(
    "OPENROUTER_API_KEY loaded=%s length=%d prefix=%s",
    bool(OPENROUTER_API_KEY),
    len(OPENROUTER_API_KEY),
    OPENROUTER_API_KEY[:8] if OPENROUTER_API_KEY else "N/A",
)

# ============================================================
# 회사 라벨 ↔ 페르소나
# ============================================================

PERSONAS: dict[str, str] = {
    "OpenAI": "당신은 범용적이고 체계적인 설명에 강합니다. 질문의 핵심을 구조적으로 정리하고, 단계적으로 이해하기 쉽게 설명하는 데 집중해서 대답하세요.",
    "Anthropic": "당신은 신중하고 다각도의 분석에 강합니다. 한 가지 결론으로 단정하기보다, 여러 관점과 trade-off를 균형 있게 짚어가며 대답하세요.",
    "Google": "당신은 실용적이고 최신 정보에 기반한 답변에 강합니다. 핵심만 간결하게 추리고, 실제로 적용 가능한 정보 위주로 대답하세요.",
    "xAI": "당신은 직설적이고 가감 없는 분석에 강합니다. 돌려 말하지 않고 핵심 의견을 명확하게, 최신 맥락을 반영해 대답하세요.",
    "Perplexity": "당신은 사실 검증과 근거 제시에 강합니다. 가능한 한 구체적인 근거나 출처가 될 만한 정보를 함께 제시하며 신뢰도 높게 대답하세요.",
}

COMPANY_LABELS: list[str] = list(PERSONAS.keys())

MODEL_MAPPING: dict[str, str] = {
    "OpenAI": "openai/gpt-oss-120b:free",
    "Anthropic": "nvidia/nemotron-3-super-120b-a12b:free",
    "Google": "google/gemma-4-26b-a4b-it:free",
    "xAI": "x-ai/grok-4-fast:free",
    "Perplexity": "openai/gpt-oss-20b:free",
}

FALLBACK_MODEL_MAPPING: dict[str, str] = {
    "OpenAI": "openai/gpt-oss-20b:free",
    "Anthropic": "google/gemma-4-26b-a4b-it:free",
    "Google": "x-ai/grok-4-fast:free",
    "xAI": "openai/gpt-oss-120b:free",
    "Perplexity": "nvidia/nemotron-3-super-120b-a12b:free",
}

LAST_RESORT_MODEL = "openai/gpt-oss-20b:free"

IS_REAL_COMPANY_MODEL: dict[str, bool] = {
    "OpenAI": True,
    "Anthropic": False,
    "Google": True,
    "xAI": True,
    "Perplexity": False,
}

# ============================================================
# 스키마
# ============================================================

class CompareRequest(BaseModel):
    message: str
    model_name: str


class DebateRequest(BaseModel):
    session_id: str
    topic: str


class DebateTurn(BaseModel):
    requested_label: str
    actual_label: str
    requested_model: str
    actual_model: str
    role: str
    content: str
    failed_candidates: list[str] = Field(default_factory=list)


class DebateSession(BaseModel):
    topic: str
    round: int = 1
    turns: list[DebateTurn] = Field(default_factory=list)


class ModelCallResult(BaseModel):
    success: bool
    content: Optional[str] = None
    requested_model: str = ""
    actual_model: str = ""
    failed_candidates: list[str] = Field(default_factory=list)


DEBATE_SESSIONS: dict[str, DebateSession] = {}

# ============================================================
# 유틸
# ============================================================

def model_candidates(label: str) -> list[str]:
    primary = MODEL_MAPPING[label]
    fallback = FALLBACK_MODEL_MAPPING.get(label, LAST_RESORT_MODEL)

    chain = [primary]
    if fallback not in chain:
        chain.append(fallback)
    if LAST_RESORT_MODEL not in chain:
        chain.append(LAST_RESORT_MODEL)
    return chain


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _retry_after_seconds(response: httpx.Response) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return min(float(retry_after), RATE_LIMIT_WAIT_CAP_SECONDS)
        except ValueError:
            pass
    return RATE_LIMIT_WAIT_SECONDS


def _build_messages(persona: str, prompt: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": persona},
        {"role": "user", "content": prompt},
    ]


def _build_opening_prompt(topic: str, previous_content: Optional[str]) -> str:
    previous_section = f"\n이전 발언:\n{previous_content}\n" if previous_content else ""
    return (
        f"{previous_section}주제: {topic}\n\n"
        "[이전 발언 내용]을 참고하되, 당신의 페르소나에 맞춰 이 주제에 대한 "
        "당신만의 강력하고 독창적인 주장을 펼치세요."
    )


def _build_rebuttal_prompt(topic: str, summary: str, previous_content: Optional[str]) -> str:
    previous_section = f"\n이전 발언:\n{previous_content}\n" if previous_content else ""
    return (
        f"【이전 토론 요약】\n{summary}\n\n{previous_section}주제: {topic}\n\n"
        "위 내용을 참고하여 당신의 페르소나에 맞춰 강력히 반박하거나 새로운 주장을 펼치세요."
    )


# ============================================================
# OpenRouter 공통 호출
# ============================================================

async def _post_openrouter(
    client: httpx.AsyncClient,
    model_id: str,
    persona: str,
    prompt: str,
    stream: bool,
) -> httpx.Response:
    payload = {
        "model": model_id,
        "messages": _build_messages(persona, prompt),
        "stream": stream,
    }
    return await client.post(OPENROUTER_URL, headers=OPENROUTER_HEADERS, json=payload)


def _should_try_next_candidate(status_code: int) -> bool:
    return status_code in (400, 404, 429)


async def _call_with_candidates(
    client: httpx.AsyncClient,
    label: str,
    persona: str,
    prompt: str,
    stream: bool,
) -> tuple[Optional[httpx.Response], str, list[str]]:
    candidates = model_candidates(label)
    failed: list[str] = []

    for model_id in candidates:
        response = await _post_openrouter(client, model_id, persona, prompt, stream)

        if response.status_code == 429:
            wait_seconds = _retry_after_seconds(response)
            logger.info(
                "rate limited label=%s model=%s waiting %.1fs",
                label,
                model_id,
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds)
            response = await _post_openrouter(client, model_id, persona, prompt, stream)

        if response.status_code == 200:
            return response, model_id, failed

        failed.append(model_id)

        if stream:
            body_preview = (await response.aread()).decode("utf-8", errors="replace")[:300]
        else:
            body_preview = response.text[:300]

        logger.warning(
            "model failed label=%s model=%s status=%s body=%s",
            label,
            model_id,
            response.status_code,
            body_preview,
        )

        if not _should_try_next_candidate(response.status_code):
            return response, model_id, failed

    return response, candidates[-1], failed  # type: ignore[has-type]


async def call_ai_model(label: str, prompt: str) -> ModelCallResult:
    persona = PERSONAS[label]
    requested_model = MODEL_MAPPING[label]

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response, actual_model, failed = await _call_with_candidates(
                client=client,
                label=label,
                persona=persona,
                prompt=prompt,
                stream=False,
            )

            if response is not None and response.status_code == 200:
                data = response.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if content:
                    return ModelCallResult(
                        success=True,
                        content=content,
                        requested_model=requested_model,
                        actual_model=actual_model,
                        failed_candidates=failed,
                    )

            return ModelCallResult(
                success=False,
                requested_model=requested_model,
                actual_model=actual_model if response is not None else requested_model,
                failed_candidates=failed,
            )

    except httpx.HTTPError as exc:
        logger.error("HTTP error calling label=%s: %s", label, exc)
        return ModelCallResult(success=False, requested_model=requested_model, actual_model=requested_model)
    except Exception as exc:  # noqa: BLE001
        logger.error("unexpected error calling label=%s: %s", label, exc)
        return ModelCallResult(success=False, requested_model=requested_model, actual_model=requested_model)


# ============================================================
# /compare/stream
# ============================================================

@app.post("/compare/stream")
async def stream_compare(data: CompareRequest):
    if data.model_name not in MODEL_MAPPING:
        raise HTTPException(status_code=400, detail="Invalid model_name")

    label = data.model_name
    persona = PERSONAS[label]
    candidates = model_candidates(label)

    async def generate():
        for model_id in candidates:
            retried_429 = False

            while True:
                try:
                    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                        async with client.stream(
                            "POST",
                            OPENROUTER_URL,
                            headers=OPENROUTER_HEADERS,
                            json={
                                "model": model_id,
                                "messages": _build_messages(persona, data.message),
                                "stream": True,
                            },
                        ) as response:
                            if response.status_code != 200:
                                body = (await response.aread()).decode("utf-8", errors="replace")[:300]
                                logger.warning(
                                    "stream failed label=%s model=%s status=%s body=%s",
                                    label,
                                    model_id,
                                    response.status_code,
                                    body,
                                )

                                if response.status_code == 429 and not retried_429:
                                    retried_429 = True
                                    await asyncio.sleep(_retry_after_seconds(response))
                                    continue

                                break

                            had_error = False
                            async for line in response.aiter_lines():
                                if not line or not line.startswith("data: "):
                                    continue
                                if line == "data: [DONE]":
                                    continue

                                try:
                                    raw = json.loads(line[6:])
                                except json.JSONDecodeError:
                                    logger.warning("stream json decode error label=%s line=%s", label, line[:200])
                                    had_error = True
                                    break

                                if raw.get("error"):
                                    logger.warning("stream inline error label=%s raw=%s", label, raw)
                                    had_error = True
                                    break

                                delta = raw.get("choices", [{}])[0].get("delta", {})
                                chunk = delta.get("content")
                                if chunk:
                                    yield _sse({"model": label, "chunk": chunk})

                            if had_error:
                                yield _sse({"model": label, "error": "응답 처리 중 오류가 발생했습니다."})
                            return

                except httpx.HTTPError as exc:
                    logger.error("stream HTTP error label=%s model=%s: %s", label, model_id, exc)
                    break

        yield _sse({"model": label, "error": "API 한도 초과 또는 오류가 발생했습니다."})

    return StreamingResponse(generate(), media_type="text/event-stream")


# ============================================================
# /models/info
# ============================================================

@app.get("/models/info")
async def models_info():
    return {
        "mapping": MODEL_MAPPING,
        "is_real_company_model": IS_REAL_COMPANY_MODEL,
    }


# ============================================================
# Debate helpers
# ============================================================

async def _produce_debate_turn(
    preferred_label: str,
    used_labels: set[str],
    prompt_builder: Callable[[str], str],
) -> Optional[DebateTurn]:
    remaining = [label for label in COMPANY_LABELS if label not in used_labels]

    if preferred_label in remaining:
        remaining.remove(preferred_label)
        candidate_labels = [preferred_label] + remaining
    else:
        candidate_labels = remaining

    candidate_labels = candidate_labels[:MAX_DEBATE_LABEL_RETRIES]
    failed_labels: list[str] = []

    for label in candidate_labels:
        result = await call_ai_model(label, prompt_builder(label))

        if result.success:
            return DebateTurn(
                requested_label=preferred_label,
                actual_label=label,
                requested_model=result.requested_model,
                actual_model=result.actual_model,
                role="독자적 주장",
                content=result.content or "",
                failed_candidates=failed_labels,
            )

        failed_labels.append(label)
        logger.info("debate turn failed label=%s, trying next candidate", label)

    logger.warning(
        "debate turn fully failed preferred_label=%s failed_labels=%s",
        preferred_label,
        failed_labels,
    )
    return None


async def _run_debate_round(
    prompt_builder: Callable[[str, Optional[str]], str],
    num_speakers: int = 3,
) -> list[DebateTurn]:
    preferred_labels = random.sample(COMPANY_LABELS, min(num_speakers, len(COMPANY_LABELS)))
    turns: list[DebateTurn] = []
    used_labels: set[str] = set()

    for preferred_label in preferred_labels:
        previous_content = turns[-1].content if turns else None

        def _prompt(label: str) -> str:
            return prompt_builder(label, previous_content)

        turn = await _produce_debate_turn(preferred_label, used_labels, _prompt)
        if turn is None:
            continue

        used_labels.add(turn.actual_label)
        turns.append(turn)
        await asyncio.sleep(0.3)

    return turns


# ============================================================
# /debate/start
# ============================================================

@app.post("/debate/start")
async def debate_start(request: DebateRequest):
    session = DebateSession(topic=request.topic, round=1)
    DEBATE_SESSIONS[request.session_id] = session

    turns = await _run_debate_round(
        prompt_builder=lambda _label, previous: _build_opening_prompt(request.topic, previous),
    )

    session.turns = turns

    return {
        "debateId": random.randint(1000, 9999),
        "selectedModels": [turn.actual_label for turn in turns],
        "turns": [turn.model_dump() for turn in turns],
        "round": 1,
    }


# ============================================================
# /debate/continue
# ============================================================

@app.post("/debate/continue")
async def debate_continue(request: DebateRequest):
    session = DEBATE_SESSIONS.get(request.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다")

    recent_turns = session.turns[-3:]
    summary = "\n".join(f"{turn.actual_label}: {turn.content[:100]}..." for turn in recent_turns)

    turns = await _run_debate_round(
        prompt_builder=lambda _label, previous: _build_rebuttal_prompt(session.topic, summary, previous),
    )

    session.round += 1
    session.turns = turns

    return {
        "debateId": random.randint(1000, 9999),
        "selectedModels": [turn.actual_label for turn in turns],
        "turns": [turn.model_dump() for turn in turns],
        "round": session.round,
        "summary": summary,
    }


# ============================================================
# 정적/헬스체크
# ============================================================

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
def serve_home():
    index_path = os.path.join(BASE_DIR, "index.html")
    return FileResponse(index_path)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
