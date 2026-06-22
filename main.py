from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from collections.abc import AsyncIterator
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
MODEL_CACHE_TTL_SECONDS = 600
MAX_MODEL_CANDIDATES_PER_LABEL = 5
HEALTHCHECK_CONCURRENCY = 2
HEALTHCHECK_DELAY_SECONDS = 1.0

# Last resort only when OpenRouter's model catalog cannot be fetched at startup.
LAST_RESORT_MODEL = "openai/gpt-oss-20b:free"

SPEECH_MODEL_WHITELIST = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-coder:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-26b-a4b-it:free",
    "deepseek/deepseek-r1:free",
]

SUMMARY_MODEL_WHITELIST = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-coder:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-26b-a4b-it:free",
    "deepseek/deepseek-r1:free",
]

META_RESPONSE_KEYWORDS = [
    "safe",
    "unsafe",
    "user safety",
    "policy",
    "classification",
    "i cannot",
    "i can't comply",
    "as an ai",
]

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
        "당신은 범용적이고 체계적인 설명에 강합니다. 핵심을 구조적으로 정리하고, "
        "한국어로 명확하고 실용적으로 답변하세요."
    ),
    "Anthropic": (
        "당신은 신중하고 다각도의 분석에 강합니다. 여러 관점과 trade-off를 균형 있게 "
        "짚으면서 한국어로 답변하세요."
    ),
    "Google": (
        "당신은 실용적이고 적용 가능한 정보 정리에 강합니다. 핵심을 간결하게 추리고 "
        "한국어로 실행 가능한 관점을 제시하세요."
    ),
    "xAI": (
        "당신은 직설적이고 날카로운 분석에 강합니다. 돌려 말하지 말고 핵심 의견을 "
        "한국어로 분명하게 제시하세요."
    ),
    "Perplexity": (
        "당신은 사실 검증과 근거 제시에 강합니다. 확인 가능한 근거와 논리 구조를 함께 "
        "한국어로 제시하세요."
    ),
}

MODEL_MAPPING: dict[str, str] = {}
FALLBACK_MODEL_MAPPING: dict[str, str] = {}
MODEL_CANDIDATES: dict[str, list[str]] = {}
IS_REAL_COMPANY_MODEL: dict[str, bool] = {}


@dataclass
class ModelCacheState:
    loaded: bool = False
    source: str = "not_loaded"
    error: str | None = None
    refreshed_at: float = 0.0
    free_model_ids: set[str] = field(default_factory=set)
    free_models_by_label: dict[str, list[str]] = field(default_factory=dict)
    all_free_models: list[str] = field(default_factory=list)


MODEL_CACHE_STATE = ModelCacheState()
MODEL_CACHE_LOCK = asyncio.Lock()


class CompareRequest(BaseModel):
    message: str
    model_name: str


class DebateRequest(BaseModel):
    session_id: str
    topic: str
    user_input: str | None = None


class DebateTurn(BaseModel):
    round_number: int
    speaker_index: int
    model: str
    actual_label: str
    requested_label: str
    actual_model: str
    requested_model: str
    is_real_company_model: bool
    role: str
    content: str
    failed_candidates: list[str] = Field(default_factory=list)


class UserInputHistoryItem(BaseModel):
    round_number: int
    content: str


class DebateSession(BaseModel):
    topic: str
    round_number: int = 0
    current_round_turns: list[DebateTurn] = Field(default_factory=list)
    round_speaker_labels: list[str] = Field(default_factory=list)
    previous_summary: str = ""
    pending_user_input: str | None = None
    user_input_history: list[UserInputHistoryItem] = Field(default_factory=list)
    failed_candidates: list[str] = Field(default_factory=list)


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
DEBATE_LOCKS: dict[str, asyncio.Lock] = {}


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


def dump_model(model: BaseModel) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()  # type: ignore[attr-defined]
    return model.dict()


def qwen_model_score(model_id: str) -> tuple[int, int, str]:
    numbers = [int(value) for value in re.findall(r"\d+", model_id)]
    biggest_number = max(numbers) if numbers else 0
    coder_bonus = 1000 if "coder" in model_id else 0
    instruct_bonus = 100 if "instruct" in model_id or "chat" in model_id else 0
    return coder_bonus + instruct_bonus + biggest_number, sum(numbers), model_id


def resolve_whitelist_model(model_id: str, free_model_ids: set[str], all_free_models: list[str]) -> str | None:
    if model_id in free_model_ids:
        return model_id
    if model_id == "qwen/qwen3-coder:free":
        qwen_candidates = [
            candidate
            for candidate in all_free_models
            if candidate.startswith("qwen/") and candidate.endswith(":free")
        ]
        if qwen_candidates:
            return sorted(qwen_candidates, key=qwen_model_score, reverse=True)[0]
    return None


def whitelist_substitutes_for_label(label: str, whitelist: list[str]) -> list[str]:
    own_prefix = COMPANY_PREFIXES[label]
    candidates: list[str] = []
    for model_id in whitelist:
        resolved_model = resolve_whitelist_model(
            model_id,
            MODEL_CACHE_STATE.free_model_ids,
            MODEL_CACHE_STATE.all_free_models,
        )
        if not resolved_model:
            continue
        if resolved_model.startswith(own_prefix):
            continue
        candidates.append(resolved_model)
    return unique(candidates)


def random_free_substitutes_for_label(label: str, limit: int) -> list[str]:
    own_prefix = COMPANY_PREFIXES[label]
    pool = [
        model_id
        for model_id in MODEL_CACHE_STATE.all_free_models
        if not model_id.startswith(own_prefix)
    ]
    if not pool:
        return []
    return random.sample(pool, min(limit, len(pool)))


def build_candidates_for_label(label: str, whitelist: list[str]) -> list[str]:
    same_company = unique(MODEL_CACHE_STATE.free_models_by_label.get(label, []))
    substitutes = whitelist_substitutes_for_label(label, whitelist)
    if not substitutes:
        substitutes = random_free_substitutes_for_label(label, MAX_MODEL_CANDIDATES_PER_LABEL)

    candidates = unique(same_company + substitutes)
    if not candidates:
        candidates = [LAST_RESORT_MODEL]
    return candidates[:MAX_MODEL_CANDIDATES_PER_LABEL]


def rebuild_model_mappings(source: str) -> None:
    MODEL_MAPPING.clear()
    FALLBACK_MODEL_MAPPING.clear()
    MODEL_CANDIDATES.clear()
    IS_REAL_COMPANY_MODEL.clear()

    for label in COMPANY_LABELS:
        candidates = build_candidates_for_label(label, SPEECH_MODEL_WHITELIST)
        MODEL_CANDIDATES[label] = candidates
        MODEL_MAPPING[label] = candidates[0]
        FALLBACK_MODEL_MAPPING[label] = candidates[1] if len(candidates) > 1 else candidates[0]
        IS_REAL_COMPANY_MODEL[label] = candidates[0].startswith(COMPANY_PREFIXES[label])

    MODEL_CACHE_STATE.loaded = True
    MODEL_CACHE_STATE.source = source
    MODEL_CACHE_STATE.refreshed_at = time.time()


async def refresh_model_cache() -> None:
    try:
        async with httpx.AsyncClient(timeout=MODEL_REFRESH_TIMEOUT) as client:
            response = await client.get(OPENROUTER_MODELS_URL)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        logger.exception("Failed to fetch OpenRouter model catalog. Using last-resort fallback.")
        MODEL_CACHE_STATE.free_model_ids = {LAST_RESORT_MODEL}
        MODEL_CACHE_STATE.all_free_models = [LAST_RESORT_MODEL]
        MODEL_CACHE_STATE.free_models_by_label = {"OpenAI": [LAST_RESORT_MODEL]}
        MODEL_CACHE_STATE.error = str(exc)
        rebuild_model_mappings("last_resort")
        return

    free_models_by_label: dict[str, list[str]] = {label: [] for label in COMPANY_LABELS}
    all_free_models: list[str] = []

    for model in payload.get("data", []):
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
        all_free_models = [LAST_RESORT_MODEL]
        free_models_by_label = {"OpenAI": [LAST_RESORT_MODEL]}

    MODEL_CACHE_STATE.free_model_ids = set(all_free_models)
    MODEL_CACHE_STATE.all_free_models = unique(all_free_models)
    MODEL_CACHE_STATE.free_models_by_label = free_models_by_label
    MODEL_CACHE_STATE.error = None
    rebuild_model_mappings("openrouter_catalog")
    logger.info("Loaded OpenRouter free model cache: %s", MODEL_CANDIDATES)


async def ensure_model_cache_fresh() -> None:
    is_empty = not MODEL_CACHE_STATE.loaded or not MODEL_CACHE_STATE.free_model_ids
    is_stale = time.time() - MODEL_CACHE_STATE.refreshed_at > MODEL_CACHE_TTL_SECONDS
    if not is_empty and not is_stale:
        return

    async with MODEL_CACHE_LOCK:
        is_empty = not MODEL_CACHE_STATE.loaded or not MODEL_CACHE_STATE.free_model_ids
        is_stale = time.time() - MODEL_CACHE_STATE.refreshed_at > MODEL_CACHE_TTL_SECONDS
        if is_empty or is_stale:
            await refresh_model_cache()


@app.on_event("startup")
async def startup() -> None:
    log_openrouter_key_status()
    await refresh_model_cache()


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def build_messages(persona: str, prompt: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": persona},
        {"role": "user", "content": prompt},
    ]


def build_chat_payload(
    model_id: str,
    persona: str,
    prompt: str,
    stream: bool,
    max_tokens: int | None = None,
) -> dict:
    payload: dict = {
        "model": model_id,
        "messages": build_messages(persona, prompt),
        "stream": stream,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return payload


def contains_korean(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text))


def is_meta_or_invalid_response(content: str, prompt: str = "") -> bool:
    stripped = content.strip()
    lowered = stripped.lower()
    has_meta_keyword = any(keyword in lowered for keyword in META_RESPONSE_KEYWORDS)

    if len(stripped) < 20 and has_meta_keyword:
        return True

    if len(stripped) >= 20 and contains_korean(prompt) and not contains_korean(stripped):
        words = re.findall(r"[a-zA-Z']+", lowered)
        only_word_like = bool(words) and re.fullmatch(r"[\s,a-zA-Z'/-]+", stripped) is not None
        meta_word_set = {
            "safe",
            "unsafe",
            "policy",
            "classification",
            "cannot",
            "comply",
            "safety",
        }
        if only_word_like and 1 <= len(words) <= 3 and all(word in meta_word_set for word in words):
            return True

    return False


def should_try_next_candidate(status_code: int) -> bool:
    return status_code in {400, 401, 403, 404, 408, 409, 429, 500, 502, 503, 504}


def extract_content(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


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


async def call_ai_model(
    label: str,
    prompt: str,
    whitelist: list[str] | None = None,
    max_tokens: int | None = None,
) -> ModelCallResult:
    if label not in COMPANY_LABELS:
        raise HTTPException(status_code=400, detail="Invalid model label")

    await ensure_model_cache_fresh()
    persona = PERSONAS[label]
    candidates = build_candidates_for_label(label, whitelist or SPEECH_MODEL_WHITELIST)
    requested_model = candidates[0]
    failed_candidates: list[str] = []

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for model_id in candidates:
            # TEMP DEBUG LOG - remove after diagnosing 429 issue
            logger.info("REQUEST label=%s model=%s", label, model_id)
            try:
                response = await client.post(
                    OPENROUTER_CHAT_URL,
                    headers=build_openrouter_headers(),
                    json=build_chat_payload(model_id, persona, prompt, stream=False, max_tokens=max_tokens),
                )
            except httpx.HTTPError as exc:
                logger.warning("OpenRouter request failed label=%s model=%s error=%s", label, model_id, exc)
                failed_candidates.append(model_id)
                continue

            # TEMP DEBUG LOG - remove after diagnosing 429 issue
            logger.info("RESPONSE label=%s model=%s status=%s", label, model_id, response.status_code)
            if response.status_code == 429:
                logger.warning("RATE_LIMITED label=%s model=%s", label, model_id)
            if response.status_code != 200:
                logger.warning(
                    "OpenRouter returned non-200 label=%s model=%s status=%s body=%s",
                    label,
                    model_id,
                    response.status_code,
                    response.text[:300],
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

            if not content.strip() or is_meta_or_invalid_response(content, prompt):
                failed_candidates.append(model_id)
                logger.warning("Invalid/meta response skipped label=%s model=%s content=%s", label, model_id, content[:80])
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


def format_prior_turns(turns: list[DebateTurn]) -> str:
    return "\n\n".join(
        f"{turn.speaker_index}번 발언자({turn.actual_label}) 원문:\n{turn.content}"
        for turn in turns
    )


def build_round_prompt(
    topic: str,
    round_number: int,
    speaker_index: int,
    prior_turns: list[DebateTurn],
    previous_summary: str,
    user_input: str | None,
) -> str:
    if round_number == 1 and speaker_index == 1:
        return (
            f"주제: {topic}\n\n"
            "당신은 1번 발언자입니다. 다른 발언자의 내용은 아직 없습니다. "
            "주제만 보고 첫 주장을 한국어로 명확하게 제시하세요."
        )

    if speaker_index == 1:
        if user_input:
            return (
                f"지금까지 토론 요약: {previous_summary or '아직 요약이 없습니다.'}\n\n"
                f"사용자가 추가로 다음 질문/의견을 남겼습니다: '{user_input}'\n\n"
                "이 질문/의견을 반드시 반영하여 답변하세요.\n\n"
                f"주제: {topic}"
            )
        return (
            f"이전 라운드 요약:\n{previous_summary or '아직 요약이 없습니다.'}\n\n"
            f"주제: {topic}\n\n"
            "당신은 새 라운드의 1번 발언자입니다. 이전 라운드 요약을 바탕으로 "
            "반복을 피하고 새로운 주장이나 관점을 한국어로 제시하세요."
        )

    role_instruction = "반박/보완" if speaker_index == 2 else "반박하거나 종합"
    return (
        f"주제: {topic}\n\n"
        f"현재 라운드에서 당신보다 앞선 모든 발언의 전체 원문입니다.\n\n"
        f"{format_prior_turns(prior_turns)}\n\n"
        f"당신은 {speaker_index}번 발언자입니다. 위 원문 전체를 직접 참고하여 "
        f"{role_instruction}하는 답변을 한국어로 제시하세요."
    )


def build_summary_prompt(existing_summary: str, turns: list[DebateTurn]) -> str:
    previous = existing_summary or "이전 누적 요약은 없습니다."
    return (
        f"기존 누적 요약:\n{previous}\n\n"
        "직전 라운드의 전체 원문입니다.\n\n"
        f"{format_prior_turns(turns)}\n\n"
        "위 내용을 의미 있게 압축해 누적 요약을 갱신하세요. "
        "'1번(라벨)은 ~라고 주장했고, 2번(라벨)은 ~라고 반박했고, 3번(라벨)은 ~라고 종합했다' "
        "형태가 드러나야 합니다. 글자수로 자르지 말고 한국어 문단으로 요약하세요."
    )


async def summarize_previous_round(session: DebateSession) -> None:
    if not session.current_round_turns:
        return

    summary_label = "OpenAI"
    result = await call_ai_model(
        summary_label,
        build_summary_prompt(session.previous_summary, session.current_round_turns),
        whitelist=SUMMARY_MODEL_WHITELIST,
        max_tokens=500,
    )
    if result.success:
        session.previous_summary = result.content
    else:
        fallback_lines = [
            f"{turn.speaker_index}번({turn.actual_label})은 {turn.content}"
            for turn in session.current_round_turns
        ]
        session.previous_summary = (session.previous_summary + "\n\n" + "\n".join(fallback_lines)).strip()
        session.failed_candidates.extend(result.failed_candidates)


def get_debate_lock(session_id: str) -> asyncio.Lock:
    if session_id not in DEBATE_LOCKS:
        DEBATE_LOCKS[session_id] = asyncio.Lock()
    return DEBATE_LOCKS[session_id]


def append_pending_user_input(session: DebateSession, content: str, target_round: int) -> None:
    clean_content = content.strip()
    if not clean_content:
        return
    if session.pending_user_input:
        session.pending_user_input = f"{session.pending_user_input}\n\n{clean_content}"
    else:
        session.pending_user_input = clean_content
    session.user_input_history.append(UserInputHistoryItem(round_number=target_round, content=clean_content))


def result_to_debate_turn(
    round_number: int,
    speaker_index: int,
    requested_label: str,
    result: ModelCallResult,
) -> DebateTurn:
    role_by_index = {
        1: "1번 주장",
        2: "2번 반박",
        3: "3번 종합",
    }
    return DebateTurn(
        round_number=round_number,
        speaker_index=speaker_index,
        model=result.actual_label,
        actual_label=result.actual_label,
        requested_label=requested_label,
        actual_model=result.actual_model,
        requested_model=result.requested_model,
        is_real_company_model=result.is_real_company_model,
        role=role_by_index.get(speaker_index, "발언"),
        content=result.content,
        failed_candidates=result.failed_candidates,
    )


async def produce_debate_speaker(
    session: DebateSession,
    round_number: int,
    speaker_index: int,
    requested_label: str,
    prior_turns: list[DebateTurn],
    round_user_input: str | None,
) -> DebateTurn | None:
    requested_labels = [requested_label] + [label for label in COMPANY_LABELS if label != requested_label]
    prompt = build_round_prompt(
        topic=session.topic,
        round_number=round_number,
        speaker_index=speaker_index,
        prior_turns=prior_turns,
        previous_summary=session.previous_summary,
        user_input=round_user_input if speaker_index == 1 else None,
    )

    for label in requested_labels[:MAX_MODEL_CANDIDATES_PER_LABEL]:
        result = await call_ai_model(label, prompt, whitelist=SPEECH_MODEL_WHITELIST)
        session.failed_candidates.extend(result.failed_candidates)
        if result.success:
            return result_to_debate_turn(round_number, speaker_index, requested_label, result)
        logger.info("Debate speaker failed requested_label=%s speaker=%s error=%s", label, speaker_index, result.error)
    return None


async def prepare_next_round_if_needed(session: DebateSession) -> str | None:
    if session.round_number == 0:
        session.round_number = 1
        session.current_round_turns = []
        session.round_speaker_labels = random.sample(COMPANY_LABELS, 3)
        round_user_input = session.pending_user_input
        session.pending_user_input = None
        return round_user_input

    if len(session.current_round_turns) < 3:
        return None

    if session.round_number > 0:
        await summarize_previous_round(session)

    session.round_number += 1
    round_user_input = session.pending_user_input
    session.pending_user_input = None
    session.current_round_turns = []
    session.round_speaker_labels = random.sample(COMPANY_LABELS, 3)
    return round_user_input


async def run_debate_step(session: DebateSession) -> list[DebateTurn]:
    round_user_input = await prepare_next_round_if_needed(session)
    speaker_index = len(session.current_round_turns) + 1
    if speaker_index > 3:
        return []

    if len(session.round_speaker_labels) < 3:
        session.round_speaker_labels = random.sample(COMPANY_LABELS, 3)

    requested_label = session.round_speaker_labels[speaker_index - 1]
    turn = await produce_debate_speaker(
        session=session,
        round_number=session.round_number,
        speaker_index=speaker_index,
        requested_label=requested_label,
        prior_turns=session.current_round_turns,
        round_user_input=round_user_input,
    )
    if turn is None:
        return []

    session.current_round_turns.append(turn)
    return [turn]


def debate_response(session: DebateSession, turns: list[DebateTurn], queued: bool = False) -> dict:
    return {
        "debateId": random.randint(1000, 9999),
        "queued": queued,
        "selectedModels": [turn.model for turn in turns],
        "turns": [dump_model(turn) for turn in turns],
        "round": session.round_number,
        "summary": session.previous_summary,
        "pending_user_input": session.pending_user_input,
        "user_input_history": [dump_model(item) for item in session.user_input_history],
        "failed_candidates": session.failed_candidates,
    }


@app.post("/compare/stream")
async def stream_compare(data: CompareRequest) -> StreamingResponse:
    if data.model_name not in COMPANY_LABELS:
        raise HTTPException(status_code=400, detail="Invalid model_name")

    await ensure_model_cache_fresh()
    label = data.model_name
    persona = PERSONAS[label]
    candidates = build_candidates_for_label(label, SPEECH_MODEL_WHITELIST)

    async def generate() -> AsyncIterator[str]:
        for model_id in candidates:
            # TEMP DEBUG LOG - remove after diagnosing 429 issue
            logger.info("REQUEST label=%s model=%s", label, model_id)
            try:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    async with client.stream(
                        "POST",
                        OPENROUTER_CHAT_URL,
                        headers=build_openrouter_headers(),
                        json=build_chat_payload(model_id, persona, data.message, stream=True),
                    ) as response:
                        # TEMP DEBUG LOG - remove after diagnosing 429 issue
                        logger.info("RESPONSE label=%s model=%s status=%s", label, model_id, response.status_code)
                        if response.status_code == 429:
                            logger.warning("RATE_LIMITED label=%s model=%s", label, model_id)
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
                                yield sse({"model": data.model_name, "error": "[API 오류 JSON 파싱 실패]"})
                                return

                            if raw.get("error"):
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


@app.post("/debate/start")
async def debate_start(request: DebateRequest) -> dict:
    await ensure_model_cache_fresh()
    session = DebateSession(topic=request.topic)
    DEBATE_SESSIONS[request.session_id] = session
    lock = get_debate_lock(request.session_id)

    async with lock:
        if request.user_input:
            append_pending_user_input(session, request.user_input, target_round=1)
        turns = await run_debate_step(session)
    return debate_response(session, turns)


@app.post("/debate/continue")
async def debate_continue(request: DebateRequest) -> dict:
    await ensure_model_cache_fresh()
    session = DEBATE_SESSIONS.get(request.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Debate session not found")

    lock = get_debate_lock(request.session_id)
    if request.user_input and (lock.locked() or len(session.current_round_turns) < 3):
        append_pending_user_input(session, request.user_input, target_round=session.round_number + 1)
        return debate_response(session, [], queued=True)

    async with lock:
        if request.user_input:
            append_pending_user_input(session, request.user_input, target_round=session.round_number + 1)
        turns = await run_debate_step(session)
    return debate_response(session, turns)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/models/info")
async def models_info() -> dict:
    await ensure_model_cache_fresh()
    return {
        "source": MODEL_CACHE_STATE.source,
        "error": MODEL_CACHE_STATE.error,
        "refreshed_at": MODEL_CACHE_STATE.refreshed_at,
        "cache_ttl_seconds": MODEL_CACHE_TTL_SECONDS,
        "mapping": MODEL_MAPPING,
        "fallbackMapping": FALLBACK_MODEL_MAPPING,
        "candidates": MODEL_CANDIDATES,
        "speech_model_whitelist": SPEECH_MODEL_WHITELIST,
        "summary_model_whitelist": SUMMARY_MODEL_WHITELIST,
        "is_real_company_model": IS_REAL_COMPANY_MODEL,
        "free_models_by_label": MODEL_CACHE_STATE.free_models_by_label,
    }


async def check_model_health(semaphore: asyncio.Semaphore, model_id: str) -> tuple[str, str]:
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
            return model_id, f"실패({exc.__class__.__name__})"

        if response.status_code == 200:
            return model_id, "정상"
        return model_id, f"실패({response.status_code})"


@app.get("/models/health")
async def models_health() -> dict[str, str]:
    await ensure_model_cache_fresh()
    semaphore = asyncio.Semaphore(HEALTHCHECK_CONCURRENCY)
    model_ids = unique([model_id for candidates in MODEL_CANDIDATES.values() for model_id in candidates])
    results = await asyncio.gather(*(check_model_health(semaphore, model_id) for model_id in model_ids))
    return dict(results)


@app.get("/")
def serve_home() -> FileResponse:
    index_path = os.path.join(BASE_DIR, "index.html")
    return FileResponse(index_path)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
