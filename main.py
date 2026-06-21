from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

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
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
MODEL_REFRESH_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_MODEL_CANDIDATES_PER_LABEL = 3
HEALTHCHECK_CONCURRENCY = 2
HEALTHCHECK_DELAY_SECONDS = 1.0

# Last resort only when OpenRouter's model catalog cannot be fetched at startup.
LAST_RESORT_MODEL = "openai/gpt-oss-20b:free"


app = FastAPI(title="ArenaX Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


COMPANY_PREFIXES: dict[str, str] = {
    "OpenAI": "openai/",
    "Anthropic": "anthropic/",
    "Google": "google/",
    "xAI": "x-ai/",
    "Perplexity": "perplexity/",
}

COMPANY_LABELS = list(COMPANY_PREFIXES.keys())

PERSONAS: dict[str, str] = {
    "OpenAI": (
        "당신은 범용적이고 체계적인 설명에 강합니다. 질문의 핵심을 구조적으로 정리하고, "
        "단계적으로 이해하기 쉽게 답변하세요."
    ),
    "Anthropic": (
        "당신은 신중하고 다각도의 분석에 강합니다. 하나의 결론으로 성급히 좁히기보다 "
        "여러 관점과 trade-off를 균형 있게 짚어가며 답변하세요."
    ),
    "Google": (
        "당신은 실용적이고 최신 정보 기반의 답변에 강합니다. 핵심을 간결하게 추리고 "
        "실제로 적용 가능한 정보 위주로 답변하세요."
    ),
    "xAI": (
        "당신은 직설적이고 날카로운 분석에 강합니다. 돌려 말하지 말고 핵심 의견을 "
        "명확하게 제시하세요."
    ),
    "Perplexity": (
        "당신은 사실 검증과 근거 제시에 강합니다. 가능한 한 구체적인 근거와 확인할 만한 "
        "정보를 함께 제시하세요."
    ),
}


MODEL_MAPPING: dict[str, str] = {}
FALLBACK_MODEL_MAPPING: dict[str, str] = {}
MODEL_CANDIDATES: dict[str, list[str]] = {}
IS_REAL_COMPANY_MODEL: dict[str, bool] = {}
MODEL_LABEL_BY_ID: dict[str, str] = {}


@dataclass
class ModelCacheState:
    loaded: bool = False
    source: str = "not_loaded"
    error: str | None = None
    free_models_by_label: dict[str, list[str]] = field(default_factory=dict)


MODEL_CACHE_STATE = ModelCacheState()


class CompareRequest(BaseModel):
    message: str
    model_name: str


class DebateRequest(BaseModel):
    session_id: str
    topic: str


class DebateTurn(BaseModel):
    model: str
    actual_label: str
    requested_label: str
    actual_model: str
    requested_model: str
    is_real_company_model: bool
    role: str
    content: str
    failed_candidates: list[str] = Field(default_factory=list)


class DebateSession(BaseModel):
    topic: str
    round: int = 1
    turns: list[DebateTurn] = Field(default_factory=list)


class ModelCallResult(BaseModel):
    success: bool
    content: str = ""
    requested_label: str
    actual_label: str
    requested_model: str
    actual_model: str
    is_real_company_model: bool
    failed_candidates: list[str] = Field(default_factory=list)
    error: str | None = None


DEBATE_SESSIONS: dict[str, DebateSession] = {}


def build_openrouter_headers() -> dict[str, str]:
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    return {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://arenax.com",
        "X-Title": "ArenaX",
        "Content-Type": "application/json",
    }


def log_openrouter_key_status() -> None:
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    prefix = api_key[:8] if api_key else "N/A"
    if not api_key:
        logger.warning("OPENROUTER_API_KEY is empty. length=0 prefix=%s", prefix)
        return
    logger.info("OPENROUTER_API_KEY loaded. length=%d prefix=%s", len(api_key), prefix)


def is_free_model(model: dict) -> bool:
    pricing = model.get("pricing") or {}
    return str(pricing.get("prompt")) == "0" and str(pricing.get("completion")) == "0"


def label_for_model_id(model_id: str) -> str:
    for label, prefix in COMPANY_PREFIXES.items():
        if model_id.startswith(prefix):
            return label
    return model_id.split("/", 1)[0] if "/" in model_id else "Unknown"


def unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def choose_distributed_substitutes(
    label: str,
    all_free_models: list[str],
    already_assigned: set[str],
    limit: int,
) -> list[str]:
    preferred = [
        model_id
        for model_id in all_free_models
        if model_id not in already_assigned and not model_id.startswith(COMPANY_PREFIXES[label])
    ]
    if len(preferred) < limit:
        preferred.extend(
            model_id
            for model_id in all_free_models
            if model_id not in preferred and not model_id.startswith(COMPANY_PREFIXES[label])
        )
    return unique(preferred)[:limit]


def apply_model_cache(free_models_by_label: dict[str, list[str]], all_free_models: list[str], source: str) -> None:
    MODEL_MAPPING.clear()
    FALLBACK_MODEL_MAPPING.clear()
    MODEL_CANDIDATES.clear()
    IS_REAL_COMPANY_MODEL.clear()
    MODEL_LABEL_BY_ID.clear()

    assigned_primary_models: set[str] = set()

    for label in COMPANY_LABELS:
        same_company_candidates = unique(free_models_by_label.get(label, []))[:MAX_MODEL_CANDIDATES_PER_LABEL]
        has_real_company_model = bool(same_company_candidates)

        if has_real_company_model:
            candidates = same_company_candidates
        else:
            candidates = choose_distributed_substitutes(
                label=label,
                all_free_models=all_free_models,
                already_assigned=assigned_primary_models,
                limit=MAX_MODEL_CANDIDATES_PER_LABEL,
            )

        if not candidates:
            candidates = [LAST_RESORT_MODEL]

        candidates = unique(candidates)[:MAX_MODEL_CANDIDATES_PER_LABEL]
        MODEL_CANDIDATES[label] = candidates
        MODEL_MAPPING[label] = candidates[0]
        FALLBACK_MODEL_MAPPING[label] = candidates[1] if len(candidates) > 1 else candidates[0]
        IS_REAL_COMPANY_MODEL[label] = has_real_company_model
        assigned_primary_models.add(candidates[0])

        for model_id in candidates:
            MODEL_LABEL_BY_ID[model_id] = label_for_model_id(model_id)

    MODEL_CACHE_STATE.loaded = True
    MODEL_CACHE_STATE.source = source
    MODEL_CACHE_STATE.error = None
    MODEL_CACHE_STATE.free_models_by_label = free_models_by_label


async def refresh_model_cache() -> None:
    try:
        async with httpx.AsyncClient(timeout=MODEL_REFRESH_TIMEOUT) as client:
            response = await client.get(OPENROUTER_MODELS_URL)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        logger.exception("Failed to fetch OpenRouter model catalog. Using last-resort fallback.")
        fallback_by_label = {"OpenAI": [LAST_RESORT_MODEL]}
        apply_model_cache(fallback_by_label, [LAST_RESORT_MODEL], source="last_resort")
        MODEL_CACHE_STATE.error = str(exc)
        return

    models = payload.get("data", [])
    free_models_by_label: dict[str, list[str]] = {label: [] for label in COMPANY_LABELS}
    all_free_models: list[str] = []

    for model in models:
        model_id = model.get("id")
        if not isinstance(model_id, str) or not is_free_model(model):
            continue

        all_free_models.append(model_id)
        for label, prefix in COMPANY_PREFIXES.items():
            if model_id.startswith(prefix):
                free_models_by_label[label].append(model_id)
                break

    if not all_free_models:
        logger.warning("OpenRouter catalog returned no free models. Using last-resort fallback.")
        fallback_by_label = {"OpenAI": [LAST_RESORT_MODEL]}
        apply_model_cache(fallback_by_label, [LAST_RESORT_MODEL], source="last_resort_empty_catalog")
        return

    apply_model_cache(free_models_by_label, unique(all_free_models), source="openrouter_catalog")
    logger.info("Loaded OpenRouter free model cache: %s", MODEL_CANDIDATES)


@app.on_event("startup")
async def startup() -> None:
    log_openrouter_key_status()
    await refresh_model_cache()


def ensure_model_cache() -> None:
    if MODEL_CACHE_STATE.loaded:
        return
    apply_model_cache({"OpenAI": [LAST_RESORT_MODEL]}, [LAST_RESORT_MODEL], source="lazy_last_resort")


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def build_messages(persona: str, prompt: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": persona},
        {"role": "user", "content": prompt},
    ]


def build_chat_payload(model_id: str, persona: str, prompt: str, stream: bool, max_tokens: int | None = None) -> dict:
    payload: dict = {
        "model": model_id,
        "messages": build_messages(persona, prompt),
        "stream": stream,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return payload


def build_opening_prompt(topic: str, previous_content: str | None) -> str:
    previous_section = f"\n이전 발언:\n{previous_content}\n" if previous_content else ""
    return (
        f"{previous_section}주제: {topic}\n\n"
        "위 내용을 참고하되, 당신의 관점에서 이 주제에 대한 명확하고 설득력 있는 첫 주장을 하세요."
    )


def build_rebuttal_prompt(topic: str, summary: str, previous_content: str | None) -> str:
    previous_section = f"\n직전 발언:\n{previous_content}\n" if previous_content else ""
    return (
        f"지금까지의 토론 요약:\n{summary}\n\n"
        f"{previous_section}주제: {topic}\n\n"
        "위 내용을 참고하여 당신의 관점에서 반박하거나 새로운 논점을 제시하세요."
    )


def candidates_for_label(label: str) -> list[str]:
    ensure_model_cache()
    return MODEL_CANDIDATES.get(label) or [LAST_RESORT_MODEL]


def should_try_next_candidate(status_code: int) -> bool:
    return status_code in {400, 401, 403, 404, 408, 409, 429, 500, 502, 503, 504}


def extract_content(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


def dump_model(model: BaseModel) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()  # type: ignore[attr-defined]
    return model.dict()


def make_failed_result(
    requested_label: str,
    requested_model: str,
    actual_model: str,
    failed_candidates: list[str],
    error: str,
) -> ModelCallResult:
    return ModelCallResult(
        success=False,
        requested_label=requested_label,
        actual_label=label_for_model_id(actual_model),
        requested_model=requested_model,
        actual_model=actual_model,
        is_real_company_model=False,
        failed_candidates=failed_candidates,
        error=error,
    )


async def call_ai_model(label: str, prompt: str) -> ModelCallResult:
    if label not in COMPANY_LABELS:
        raise HTTPException(status_code=400, detail="Invalid model label")

    persona = PERSONAS[label]
    candidates = candidates_for_label(label)
    requested_model = candidates[0]
    failed_candidates: list[str] = []

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for model_id in candidates:
            try:
                response = await client.post(
                    OPENROUTER_CHAT_URL,
                    headers=build_openrouter_headers(),
                    json=build_chat_payload(model_id, persona, prompt, stream=False),
                )
            except httpx.HTTPError as exc:
                logger.warning("OpenRouter request failed label=%s model=%s error=%s", label, model_id, exc)
                failed_candidates.append(model_id)
                continue

            if response.status_code != 200:
                body_preview = response.text[:300]
                logger.warning(
                    "OpenRouter returned non-200 label=%s model=%s status=%s body=%s",
                    label,
                    model_id,
                    response.status_code,
                    body_preview,
                )
                failed_candidates.append(model_id)
                if should_try_next_candidate(response.status_code):
                    continue
                return make_failed_result(
                    requested_label=label,
                    requested_model=requested_model,
                    actual_model=model_id,
                    failed_candidates=failed_candidates,
                    error=f"[API 오류 {response.status_code}]",
                )

            try:
                content = extract_content(response.json())
            except json.JSONDecodeError as exc:
                logger.warning("OpenRouter JSON parse failed label=%s model=%s error=%s", label, model_id, exc)
                failed_candidates.append(model_id)
                continue

            if not content.strip():
                failed_candidates.append(model_id)
                continue

            actual_label = label_for_model_id(model_id)
            return ModelCallResult(
                success=True,
                content=content,
                requested_label=label,
                actual_label=actual_label,
                requested_model=requested_model,
                actual_model=model_id,
                is_real_company_model=model_id.startswith(COMPANY_PREFIXES[label]),
                failed_candidates=failed_candidates,
            )

    return make_failed_result(
        requested_label=label,
        requested_model=requested_model,
        actual_model=failed_candidates[-1] if failed_candidates else requested_model,
        failed_candidates=failed_candidates,
        error="All model candidates failed",
    )


@app.post("/compare/stream")
async def stream_compare(data: CompareRequest) -> StreamingResponse:
    if data.model_name not in COMPANY_LABELS:
        raise HTTPException(status_code=400, detail="Invalid model_name")

    label = data.model_name
    persona = PERSONAS[label]
    candidates = candidates_for_label(label)

    async def generate() -> AsyncIterator[str]:
        for model_id in candidates:
            try:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    async with client.stream(
                        "POST",
                        OPENROUTER_CHAT_URL,
                        headers=build_openrouter_headers(),
                        json=build_chat_payload(model_id, persona, data.message, stream=True),
                    ) as response:
                        if response.status_code != 200:
                            logger.warning(
                                "OpenRouter stream returned non-200 label=%s model=%s status=%s",
                                label,
                                model_id,
                                response.status_code,
                            )
                            if should_try_next_candidate(response.status_code) and model_id != candidates[-1]:
                                await response.aread()
                                continue
                            yield sse({"model": data.model_name, "error": f"[API 오류 {response.status_code}]"})
                            return

                        async for line in response.aiter_lines():
                            if not line or not line.startswith("data: "):
                                continue
                            if line == "data: [DONE]":
                                return

                            try:
                                raw = json.loads(line[6:])
                            except json.JSONDecodeError:
                                logger.warning("Stream JSON parse failed label=%s model=%s line=%s", label, model_id, line[:200])
                                yield sse({"model": data.model_name, "error": "[API 오류 JSON 파싱 실패]"})
                                return

                            if raw.get("error"):
                                logger.warning("OpenRouter stream payload error label=%s model=%s raw=%s", label, model_id, raw)
                                yield sse({"model": data.model_name, "error": "[API 오류 스트리밍 실패]"})
                                return

                            delta = (raw.get("choices") or [{}])[0].get("delta") or {}
                            chunk = delta.get("content")
                            if chunk:
                                yield sse(
                                    {
                                        "model": data.model_name,
                                        "actual_label": label_for_model_id(model_id),
                                        "actual_model": model_id,
                                        "is_real_company_model": model_id.startswith(COMPANY_PREFIXES[label]),
                                        "chunk": chunk,
                                    }
                                )
                        return
            except httpx.HTTPError as exc:
                logger.warning("OpenRouter stream network error label=%s model=%s error=%s", label, model_id, exc)
                if model_id != candidates[-1]:
                    continue
                yield sse({"model": data.model_name, "error": f"[API 오류 {exc.__class__.__name__}]"})
                return
            except Exception as exc:
                logger.exception("Unexpected stream error label=%s model=%s", label, model_id)
                yield sse({"model": data.model_name, "error": f"[API 오류 {exc.__class__.__name__}]"})
                return

        yield sse({"model": data.model_name, "error": "[API 오류 모든 후보 실패]"})

    return StreamingResponse(generate(), media_type="text/event-stream")


def result_to_debate_turn(preferred_label: str, result: ModelCallResult) -> DebateTurn:
    return DebateTurn(
        model=result.actual_label,
        actual_label=result.actual_label,
        requested_label=preferred_label,
        actual_model=result.actual_model,
        requested_model=result.requested_model,
        is_real_company_model=result.is_real_company_model,
        role="입장",
        content=result.content,
        failed_candidates=result.failed_candidates,
    )


async def produce_debate_turn(
    preferred_label: str,
    used_actual_labels: set[str],
    prompt_builder: Callable[[str], str],
) -> DebateTurn | None:
    requested_labels = [preferred_label] + [label for label in COMPANY_LABELS if label != preferred_label]
    requested_labels = requested_labels[:MAX_MODEL_CANDIDATES_PER_LABEL]

    for requested_label in requested_labels:
        result = await call_ai_model(requested_label, prompt_builder(requested_label))
        if result.success and result.actual_label not in used_actual_labels:
            return result_to_debate_turn(preferred_label, result)
        if result.success:
            logger.info(
                "Skipping debate turn because actual label is already used: requested=%s actual=%s",
                requested_label,
                result.actual_label,
            )
        else:
            logger.info("Skipping failed debate candidate requested=%s error=%s", requested_label, result.error)

    return None


async def run_debate_round(
    prompt_builder: Callable[[str, str | None], str],
    num_speakers: int = 3,
) -> list[DebateTurn]:
    preferred_labels = random.sample(COMPANY_LABELS, min(num_speakers, len(COMPANY_LABELS)))
    turns: list[DebateTurn] = []
    used_actual_labels: set[str] = set()

    for preferred_label in preferred_labels:
        previous_content = turns[-1].content if turns else None

        def build_prompt(label: str) -> str:
            return prompt_builder(label, previous_content)

        turn = await produce_debate_turn(preferred_label, used_actual_labels, build_prompt)
        if turn is not None:
            used_actual_labels.add(turn.actual_label)
            turns.append(turn)
        await asyncio.sleep(0.3)

    return turns


@app.post("/debate/start")
async def debate_start(request: DebateRequest) -> dict:
    session = DebateSession(topic=request.topic, round=1)
    DEBATE_SESSIONS[request.session_id] = session

    turns = await run_debate_round(
        prompt_builder=lambda _label, previous: build_opening_prompt(request.topic, previous),
    )
    session.turns = turns

    return {
        "debateId": random.randint(1000, 9999),
        "selectedModels": [turn.model for turn in turns],
        "turns": [dump_model(turn) for turn in turns],
        "round": session.round,
    }


@app.post("/debate/continue")
async def debate_continue(request: DebateRequest) -> dict:
    session = DEBATE_SESSIONS.get(request.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Debate session not found")

    recent_turns = session.turns[-3:]
    summary = "\n".join(f"{turn.actual_label}: {turn.content[:120]}..." for turn in recent_turns)
    turns = await run_debate_round(
        prompt_builder=lambda _label, previous: build_rebuttal_prompt(session.topic, summary, previous),
    )

    session.round += 1
    session.turns.extend(turns)

    return {
        "debateId": random.randint(1000, 9999),
        "selectedModels": [turn.model for turn in turns],
        "turns": [dump_model(turn) for turn in turns],
        "round": session.round,
        "summary": summary,
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/models/info")
async def models_info() -> dict:
    ensure_model_cache()
    return {
        "source": MODEL_CACHE_STATE.source,
        "error": MODEL_CACHE_STATE.error,
        "mapping": MODEL_MAPPING,
        "fallbackMapping": FALLBACK_MODEL_MAPPING,
        "candidates": MODEL_CANDIDATES,
        "is_real_company_model": IS_REAL_COMPANY_MODEL,
        "free_models_by_label": MODEL_CACHE_STATE.free_models_by_label,
    }


async def check_model_health(semaphore: asyncio.Semaphore, label: str, model_id: str) -> tuple[str, str]:
    async with semaphore:
        await asyncio.sleep(HEALTHCHECK_DELAY_SECONDS)
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.post(
                    OPENROUTER_CHAT_URL,
                    headers=build_openrouter_headers(),
                    json={
                        "model": model_id,
                        "max_tokens": 5,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
        except httpx.HTTPError as exc:
            logger.warning("Model health check failed label=%s model=%s error=%s", label, model_id, exc)
            return model_id, f"실패({exc.__class__.__name__})"

        if response.status_code == 200:
            return model_id, "정상"
        return model_id, f"실패({response.status_code})"


@app.get("/models/health")
async def models_health() -> dict[str, str]:
    ensure_model_cache()
    semaphore = asyncio.Semaphore(HEALTHCHECK_CONCURRENCY)
    unique_model_ids = unique([model_id for candidates in MODEL_CANDIDATES.values() for model_id in candidates])
    tasks = [
        check_model_health(semaphore, label_for_model_id(model_id), model_id)
        for model_id in unique_model_ids
    ]
    results = await asyncio.gather(*tasks)
    return dict(results)


@app.get("/")
def serve_home() -> FileResponse:
    index_path = os.path.join(BASE_DIR, "index.html")
    return FileResponse(index_path)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
