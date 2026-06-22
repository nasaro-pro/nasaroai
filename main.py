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
# Allow up to 10 candidates per label so a label can keep falling back to other
# free models even when several are rate-limited or out of context budget.
MAX_MODEL_CANDIDATES_PER_LABEL = 10
MIN_MODEL_CANDIDATES_PER_LABEL = 5
HEALTHCHECK_CONCURRENCY = 2
HEALTHCHECK_DELAY_SECONDS = 1.0
# Wait this long before retrying the exact same model once after a 429.
RATE_LIMIT_RETRY_DELAY_SECONDS = 1.0
# Simple prompt used for the very last fallback so even a tiny model can answer.
FINAL_ATTEMPT_PROMPT = "주제에 대해 한 문장으로 짧게 의견을 말해주세요."
USER_FACING_FAILURE_MSG = "이번 발언 생성에 실패했습니다. 다시 시도해주세요."
COMPARE_FAILURE_MSG = "이번 응답 생성에 실패했습니다. 다시 시도해주세요."

# Last resort only when OpenRouter's model catalog cannot be fetched at startup.
LAST_RESORT_MODEL = "openai/gpt-oss-20b:free"

SUMMARY_MODEL_WHITELIST = [
    "openai/gpt-oss-120b:free",
    "meta-llama/llama-4-maverick:free",
    "nvidia/nemotron-ultra-253b-v1:free",
    "deepseek/deepseek-r1:free",
    "qwen/qwen3-coder:free",
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
    "DeepSeek": "deepseek/",
}

COMPANY_LABELS = list(COMPANY_PREFIXES.keys())
MODELS = ["OpenAI", "Anthropic", "Google", "xAI", "Perplexity", "DeepSeek"]

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
    "DeepSeek": (
        "당신은 심층 추론과 수학적 분석에 강합니다. 단계적 사고와 논리적 근거를 명확히 하면서 "
        "한국어로 정확하고 깊이 있는 답변을 제시하세요."
    ),
}

MODEL_MAPPING: dict[str, str] = {}
FALLBACK_MODEL_MAPPING: dict[str, str] = {}
MODEL_CANDIDATES: dict[str, list[str]] = {}
IS_REAL_COMPANY_MODEL: dict[str, bool] = {}


@dataclass(frozen=True)
class LabelProviderConfig:
    """Single place to edit when switching a label to an official paid API.

    Today every label uses provider='openrouter'. To move OpenAI to the official API,
    change provider to 'openai', set official_model_id (e.g. 'gpt-4o'), and point
    official_api_base_url at OpenAI's endpoint — request_chat_completion routes there.
    """

    label: str
    provider: str = "openrouter"
    official_model_id: str | None = None
    official_api_base_url: str | None = None
    substitute_chain: tuple[str, ...] = ()


# Fixed substitute preferences when a label has no free company model in the catalog.
LABEL_PROVIDER_CONFIG: dict[str, LabelProviderConfig] = {
    "OpenAI": LabelProviderConfig(
        label="OpenAI",
        substitute_chain=(
            "openai/gpt-oss-120b:free",
            "openai/gpt-oss-20b:free",
            "meta-llama/llama-4-maverick:free",
            "nvidia/nemotron-ultra-253b-v1:free",
            "deepseek/deepseek-r1:free",
        ),
    ),
    "Anthropic": LabelProviderConfig(
        label="Anthropic",
        substitute_chain=(
            "nvidia/nemotron-ultra-253b-v1:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "meta-llama/llama-4-maverick:free",
            "deepseek/deepseek-r1:free",
            "qwen/qwen3-coder:free",
        ),
    ),
    "Google": LabelProviderConfig(
        label="Google",
        substitute_chain=(
            "meta-llama/llama-4-maverick:free",
            "meta-llama/llama-4-scout:free",
            "qwen/qwen3-coder:free",
            "deepseek/deepseek-v3:free",
            "openai/gpt-oss-120b:free",
        ),
    ),
    "xAI": LabelProviderConfig(
        label="xAI",
        substitute_chain=(
            "deepseek/deepseek-r1:free",
            "deepseek/deepseek-v3:free",
            "meta-llama/llama-4-maverick:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "openai/gpt-oss-20b:free",
        ),
    ),
    "Perplexity": LabelProviderConfig(
        label="Perplexity",
        substitute_chain=(
            "nousresearch/hermes-3-llama-3.1-70b:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "deepseek/deepseek-v3:free",
            "qwen/qwen3-coder:free",
            "google/gemma-3-27b-it:free",
        ),
    ),
    "DeepSeek": LabelProviderConfig(
        label="DeepSeek",
        substitute_chain=(
            "deepseek/deepseek-r1:free",
            "deepseek/deepseek-v3:free",
            "openai/gpt-oss-120b:free",
            "meta-llama/llama-4-maverick:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
        ),
    ),
}


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
    compare_session_id: str = ""


class DebateRequest(BaseModel):
    session_id: str
    topic: str
    user_input: str | None = None
    retry_speaker_index: int | None = None


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
    success: bool = True
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
COMPARE_ACTIVE_MODELS: dict[str, set[str]] = {}
COMPARE_SESSION_LOCK = asyncio.Lock()


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


def model_size_score(model_id: str) -> int:
    numbers = [int(value) for value in re.findall(r"\d+", model_id)]
    return max(numbers) if numbers else 0


def largest_free_model_for_prefix(prefix: str) -> str | None:
    models = [model_id for model_id in MODEL_CACHE_STATE.all_free_models if model_id.startswith(prefix)]
    if not models:
        return None
    return sorted(models, key=model_size_score, reverse=True)[0]


def resolve_catalog_model(model_id: str) -> str | None:
    if model_id in MODEL_CACHE_STATE.free_model_ids:
        return model_id
    if model_id == "qwen/qwen3-coder:free":
        qwen_candidates = [
            candidate
            for candidate in MODEL_CACHE_STATE.all_free_models
            if candidate.startswith("qwen/") and candidate.endswith(":free")
        ]
        if qwen_candidates:
            return sorted(qwen_candidates, key=qwen_model_score, reverse=True)[0]
    prefix = model_id.split("/", 1)[0] + "/" if "/" in model_id else ""
    if prefix:
        return largest_free_model_for_prefix(prefix)
    return None


def build_model_chain_for_label(label: str) -> list[str]:
    """Ordered preference chain for one label: own-company free models first, then substitutes."""
    prefix = COMPANY_PREFIXES[label]
    chain: list[str] = []

    company_model = largest_free_model_for_prefix(prefix)
    if company_model:
        chain.append(company_model)

    for substitute in LABEL_PROVIDER_CONFIG[label].substitute_chain:
        resolved = resolve_catalog_model(substitute)
        if resolved:
            chain.append(resolved)

    own_prefix = COMPANY_PREFIXES[label]
    extras = [
        model_id
        for model_id in sorted(MODEL_CACHE_STATE.all_free_models, key=model_size_score, reverse=True)
        if not model_id.startswith(own_prefix)
    ]
    chain.extend(extras)
    return unique(chain)


def assign_models_without_overlap() -> dict[str, list[str]]:
    """Pick 1st/2nd/3rd models per label so primary assignments never collide."""
    raw_chains = {label: build_model_chain_for_label(label) for label in COMPANY_LABELS}
    assigned: dict[str, list[str]] = {label: [] for label in COMPANY_LABELS}
    globally_used: set[str] = set()

    for tier in range(3):
        for label in COMPANY_LABELS:
            for model_id in raw_chains[label]:
                if model_id in assigned[label] or model_id in globally_used:
                    continue
                assigned[label].append(model_id)
                globally_used.add(model_id)
                break

    for label in COMPANY_LABELS:
        if not assigned[label]:
            for model_id in raw_chains[label]:
                if model_id not in globally_used:
                    assigned[label].append(model_id)
                    globally_used.add(model_id)
                    break
            if not assigned[label]:
                assigned[label].append(LAST_RESORT_MODEL)
                globally_used.add(LAST_RESORT_MODEL)

        for model_id in raw_chains[label]:
            if len(assigned[label]) >= MAX_MODEL_CANDIDATES_PER_LABEL:
                break
            if model_id not in assigned[label] and model_id not in globally_used:
                assigned[label].append(model_id)
                globally_used.add(model_id)

        while len(assigned[label]) < MIN_MODEL_CANDIDATES_PER_LABEL:
            pool = [
                model_id
                for model_id in MODEL_CACHE_STATE.all_free_models
                if model_id not in assigned[label] and model_id not in globally_used
            ]
            if not pool:
                if LAST_RESORT_MODEL not in assigned[label]:
                    assigned[label].append(LAST_RESORT_MODEL)
                break
            assigned[label].append(pool[0])
            globally_used.add(pool[0])

        if not assigned[label]:
            assigned[label] = [LAST_RESORT_MODEL]

    primaries = {label: models[0] for label, models in assigned.items() if models}
    if len(set(primaries.values())) != len(primaries):
        logger.warning("Primary model overlap detected after assignment: %s", primaries)
    else:
        logger.info("Unique primary model assignment: %s", primaries)

    return assigned


def resolve_label_models(label: str) -> list[str]:
    """Public resolver entry point — returns ordered model candidates for a UI label."""
    if label not in MODEL_CANDIDATES or not MODEL_CANDIDATES[label]:
        return [LAST_RESORT_MODEL]
    return list(MODEL_CANDIDATES[label])


def get_label_provider_config(label: str) -> LabelProviderConfig:
    return LABEL_PROVIDER_CONFIG[label]


def rebuild_model_mappings(source: str) -> None:
    MODEL_MAPPING.clear()
    FALLBACK_MODEL_MAPPING.clear()
    MODEL_CANDIDATES.clear()
    IS_REAL_COMPANY_MODEL.clear()

    assignments = assign_models_without_overlap()
    for label in COMPANY_LABELS:
        candidates = assignments[label][:MAX_MODEL_CANDIDATES_PER_LABEL]
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


async def request_chat_completion(
    client: httpx.AsyncClient,
    label: str,
    model_id: str,
    persona: str,
    prompt: str,
    max_tokens: int | None,
) -> httpx.Response:
    config = get_label_provider_config(label)
    if config.provider != "openrouter" and config.official_model_id and config.official_api_base_url:
        url = config.official_api_base_url
        model = config.official_model_id
    else:
        url = OPENROUTER_CHAT_URL
        model = model_id

    return await client.post(
        url,
        headers=build_openrouter_headers(),
        json=build_chat_payload(model, persona, prompt, stream=False, max_tokens=max_tokens),
    )


async def final_attempt_model_call(
    label: str,
    requested_model: str,
    failed_candidates: list[str],
) -> ModelCallResult | None:
    """Last resort: a short, simple prompt to LAST_RESORT_MODEL so the user still gets something."""
    persona = PERSONAS[label]
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await request_chat_completion(
                client, label, LAST_RESORT_MODEL, persona, FINAL_ATTEMPT_PROMPT, max_tokens=200
            )
    except httpx.HTTPError as exc:
        logger.warning("Final attempt request failed label=%s error=%s", label, exc)
        return None

    if response.status_code != 200:
        logger.warning("Final attempt non-200 label=%s status=%s", label, response.status_code)
        return None

    try:
        content = extract_content(response.json())
    except json.JSONDecodeError:
        return None

    if not content.strip():
        return None

    logger.info("Final attempt succeeded label=%s model=%s", label, LAST_RESORT_MODEL)
    return ModelCallResult(
        success=True,
        content=content,
        requested_label=label,
        actual_label=label_for_model_id(LAST_RESORT_MODEL),
        requested_model=requested_model,
        actual_model=LAST_RESORT_MODEL,
        is_real_company_model=False,
        failed_candidates=failed_candidates,
    )


async def call_ai_model(
    label: str,
    prompt: str,
    max_tokens: int | None = None,
    excluded_models: set[str] | None = None,
) -> ModelCallResult:
    if label not in COMPANY_LABELS:
        raise HTTPException(status_code=400, detail="Invalid model label")

    await ensure_model_cache_fresh()
    persona = PERSONAS[label]
    candidates = resolve_label_models(label)
    if excluded_models:
        filtered = [model_id for model_id in candidates if model_id not in excluded_models]
        candidates = filtered if filtered else candidates
    requested_model = candidates[0]
    failed_candidates: list[str] = []

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for model_id in candidates:
            try:
                response = await request_chat_completion(client, label, model_id, persona, prompt, max_tokens)
            except httpx.HTTPError as exc:
                logger.warning("OpenRouter request failed label=%s model=%s error=%s", label, model_id, exc)
                failed_candidates.append(model_id)
                continue

            # On 429, wait briefly and retry the SAME model once before moving on.
            if response.status_code == 429:
                logger.warning(
                    "Rate limited, retrying once after %ss label=%s model=%s",
                    RATE_LIMIT_RETRY_DELAY_SECONDS,
                    label,
                    model_id,
                )
                await asyncio.sleep(RATE_LIMIT_RETRY_DELAY_SECONDS)
                try:
                    response = await request_chat_completion(client, label, model_id, persona, prompt, max_tokens)
                except httpx.HTTPError as exc:
                    logger.warning("OpenRouter retry failed label=%s model=%s error=%s", label, model_id, exc)
                    failed_candidates.append(model_id)
                    continue

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
                break

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

    final_result = await final_attempt_model_call(label, requested_model, failed_candidates)
    if final_result is not None:
        return final_result

    return make_failed_result(
        requested_label=label,
        requested_model=requested_model,
        actual_model=failed_candidates[-1] if failed_candidates else requested_model,
        failed_candidates=failed_candidates,
        error=USER_FACING_FAILURE_MSG,
    )


def compress_turn_content(text: str, max_sentences: int = 2, max_chars: int = 180) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?。!?])\s+", clean)
    summary = " ".join(sentences[:max_sentences]).strip() or clean
    if len(summary) > max_chars:
        summary = summary[:max_chars].rstrip() + "..."
    return summary


def format_prior_turns(turns: list[DebateTurn], compress_older: bool = False) -> str:
    # When there are several prior turns, keep only the most recent one verbatim and
    # compress the earlier ones so the prompt does not grow without bound.
    if not compress_older or len(turns) <= 1:
        return "\n\n".join(
            f"{turn.speaker_index}번 발언자({turn.actual_label}) 원문:\n{turn.content}"
            for turn in turns
        )

    *older, latest = turns
    parts = [
        f"{turn.speaker_index}번 발언자({turn.actual_label}) 요약:\n{compress_turn_content(turn.content)}"
        for turn in older
    ]
    parts.append(f"{latest.speaker_index}번 발언자({latest.actual_label}) 원문:\n{latest.content}")
    return "\n\n".join(parts)


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
    # From the point there are multiple prior speeches, compress older ones to keep
    # the prompt within free-model context limits.
    compress_older = len(prior_turns) >= 2
    prior_text = format_prior_turns(prior_turns, compress_older=compress_older)
    return (
        f"주제: {topic}\n\n"
        f"현재 라운드에서 당신보다 앞선 발언입니다.\n\n"
        f"{prior_text}\n\n"
        f"당신은 {speaker_index}번 발언자입니다. 위 발언을 참고하여 "
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
    successful_turns = [turn for turn in session.current_round_turns if turn.success]
    if not successful_turns:
        return

    summary_label = (
        session.current_round_turns[-1].requested_label
        if session.current_round_turns
        else random.choice(COMPANY_LABELS)
    )
    result = await call_ai_model(
        summary_label,
        build_summary_prompt(session.previous_summary, successful_turns),
        max_tokens=500,
    )
    if result.success:
        session.previous_summary = result.content
    else:
        fallback_lines = [
            f"{turn.speaker_index}번({turn.actual_label})은 {turn.content}"
            for turn in successful_turns
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


ROLE_BY_INDEX = {
    1: "1번 주장",
    2: "2번 반박",
    3: "3번 종합",
}


def result_to_debate_turn(
    round_number: int,
    speaker_index: int,
    requested_label: str,
    result: ModelCallResult,
) -> DebateTurn:
    return DebateTurn(
        round_number=round_number,
        speaker_index=speaker_index,
        model=result.actual_label,
        actual_label=result.actual_label,
        requested_label=requested_label,
        actual_model=result.actual_model,
        requested_model=result.requested_model,
        is_real_company_model=result.is_real_company_model,
        role=ROLE_BY_INDEX.get(speaker_index, "발언"),
        content=result.content,
        success=result.success,
        failed_candidates=result.failed_candidates,
    )


def make_failure_turn(round_number: int, speaker_index: int, requested_label: str) -> DebateTurn:
    return DebateTurn(
        round_number=round_number,
        speaker_index=speaker_index,
        model=requested_label,
        actual_label=requested_label,
        requested_label=requested_label,
        actual_model="",
        requested_model="",
        is_real_company_model=False,
        role=ROLE_BY_INDEX.get(speaker_index, "발언"),
        content=USER_FACING_FAILURE_MSG,
        success=False,
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
        already_used_models = {turn.actual_model for turn in prior_turns if turn.actual_model}
        result = await call_ai_model(label, prompt, excluded_models=already_used_models)
        session.failed_candidates.extend(result.failed_candidates)
        if result.success:
            return result_to_debate_turn(round_number, speaker_index, requested_label, result)
        logger.info("Debate speaker failed requested_label=%s speaker=%s error=%s", label, speaker_index, result.error)
    return None


def pick_speaker_labels_avoiding_repeat(previous_labels: list[str]) -> list[str]:
    prev_first = previous_labels[0] if previous_labels else None
    for _ in range(20):
        sample = random.sample(COMPANY_LABELS, 3)
        if sample[0] != prev_first:
            return sample
    return random.sample(COMPANY_LABELS, 3)


async def prepare_next_round_if_needed(session: DebateSession) -> str | None:
    if session.round_number == 0:
        session.round_number = 1
        session.current_round_turns = []
        session.round_speaker_labels = pick_speaker_labels_avoiding_repeat(session.round_speaker_labels)
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
    session.round_speaker_labels = pick_speaker_labels_avoiding_repeat(session.round_speaker_labels)
    return round_user_input


async def run_debate_step(session: DebateSession) -> list[DebateTurn]:
    round_user_input = await prepare_next_round_if_needed(session)
    speaker_index = len(session.current_round_turns) + 1
    if speaker_index > 3:
        return []

    if len(session.round_speaker_labels) < 3:
        session.round_speaker_labels = pick_speaker_labels_avoiding_repeat(session.round_speaker_labels)

    requested_label = session.round_speaker_labels[speaker_index - 1]
    turn = await produce_debate_speaker(
        session=session,
        round_number=session.round_number,
        speaker_index=speaker_index,
        requested_label=requested_label,
        # Only feed successful prior speeches into the next prompt.
        prior_turns=[t for t in session.current_round_turns if t.success],
        round_user_input=round_user_input,
    )
    # Never return an empty turns list: a failed speaker becomes an explicit failure
    # turn so the frontend always has something to render instead of a stuck card.
    if turn is None:
        turn = make_failure_turn(session.round_number, speaker_index, requested_label)

    session.current_round_turns.append(turn)
    return [turn]


async def retry_debate_speaker(session: DebateSession, speaker_index: int) -> list[DebateTurn]:
    if speaker_index < 1 or speaker_index > 3:
        return []

    if len(session.round_speaker_labels) >= speaker_index:
        requested_label = session.round_speaker_labels[speaker_index - 1]
    else:
        requested_label = random.choice(COMPANY_LABELS)

    prior_turns = [
        turn for turn in session.current_round_turns if turn.speaker_index < speaker_index and turn.success
    ]
    turn = await produce_debate_speaker(
        session=session,
        round_number=session.round_number,
        speaker_index=speaker_index,
        requested_label=requested_label,
        prior_turns=prior_turns,
        round_user_input=None,
    )
    if turn is None:
        turn = make_failure_turn(session.round_number, speaker_index, requested_label)

    # Replace the existing (failed) turn for this speaker slot, keeping order by index.
    session.current_round_turns = [
        existing for existing in session.current_round_turns if existing.speaker_index != speaker_index
    ]
    session.current_round_turns.append(turn)
    session.current_round_turns.sort(key=lambda existing: existing.speaker_index)
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


async def acquire_compare_model(session_id: str, candidates: list[str]) -> str | None:
    if not session_id:
        return candidates[0] if candidates else None
    async with COMPARE_SESSION_LOCK:
        used = COMPARE_ACTIVE_MODELS.setdefault(session_id, set())
        for model_id in candidates:
            if model_id not in used:
                used.add(model_id)
                return model_id
    return None


async def release_compare_model(session_id: str, model_id: str) -> None:
    if not session_id or not model_id:
        return
    async with COMPARE_SESSION_LOCK:
        bucket = COMPARE_ACTIVE_MODELS.get(session_id)
        if bucket is not None:
            bucket.discard(model_id)
            if not bucket:
                COMPARE_ACTIVE_MODELS.pop(session_id, None)


@app.post("/compare/stream")
async def stream_compare(data: CompareRequest) -> StreamingResponse:
    if data.model_name not in COMPANY_LABELS:
        raise HTTPException(status_code=400, detail="Invalid model_name")

    await ensure_model_cache_fresh()
    label = data.model_name
    persona = PERSONAS[label]
    candidates = resolve_label_models(label)

    async def generate() -> AsyncIterator[str]:
        payload = build_chat_payload(model_id="", persona=persona, prompt=data.message, stream=True)
        session_id = data.compare_session_id.strip()
        excluded_by_failure: set[str] = set()

        while True:
            pool = [model_id for model_id in candidates if model_id not in excluded_by_failure]
            if not pool:
                yield sse({"model": data.model_name, "success": False, "error": COMPARE_FAILURE_MSG})
                return

            model_id = await acquire_compare_model(session_id, pool)
            if model_id is None:
                yield sse({"model": data.model_name, "success": False, "error": COMPARE_FAILURE_MSG})
                return

            try:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    for attempt in range(2):
                        async with client.stream(
                            "POST",
                            OPENROUTER_CHAT_URL,
                            headers=build_openrouter_headers(),
                            json={**payload, "model": model_id},
                        ) as response:
                            if response.status_code == 429 and attempt == 0:
                                await response.aread()
                                await asyncio.sleep(RATE_LIMIT_RETRY_DELAY_SECONDS)
                                continue

                            if response.status_code != 200:
                                logger.warning(
                                    "OpenRouter stream returned non-200 label=%s model=%s status=%s",
                                    label,
                                    model_id,
                                    response.status_code,
                                )
                                if should_try_next_candidate(response.status_code):
                                    await response.aread()
                                    excluded_by_failure.add(model_id)
                                    break
                                yield sse({"model": data.model_name, "success": False, "error": COMPARE_FAILURE_MSG})
                                return

                            async for line in response.aiter_lines():
                                if not line or not line.startswith("data: "):
                                    continue
                                if line == "data: [DONE]":
                                    return
                                try:
                                    raw = json.loads(line[6:])
                                except json.JSONDecodeError:
                                    yield sse({"model": data.model_name, "success": False, "error": COMPARE_FAILURE_MSG})
                                    return

                                if raw.get("error"):
                                    yield sse({"model": data.model_name, "success": False, "error": COMPARE_FAILURE_MSG})
                                    return

                                delta = (raw.get("choices") or [{}])[0].get("delta") or {}
                                chunk = delta.get("content")
                                if chunk:
                                    yield sse(
                                        {
                                            "model": data.model_name,
                                            "success": True,
                                            "actual_label": label_for_model_id(model_id),
                                            "actual_model": model_id,
                                            "is_real_company_model": model_id.startswith(COMPANY_PREFIXES[label]),
                                            "chunk": chunk,
                                        }
                                    )
                            return
            except httpx.HTTPError as exc:
                logger.warning("OpenRouter stream request failed label=%s model=%s error=%s", label, model_id, exc)
                excluded_by_failure.add(model_id)
            except Exception:
                logger.exception("Unexpected stream error label=%s model=%s", label, model_id)
                yield sse({"model": data.model_name, "success": False, "error": COMPARE_FAILURE_MSG})
                return
            finally:
                await release_compare_model(session_id, model_id)

            if model_id in excluded_by_failure:
                continue

            yield sse({"model": data.model_name, "success": False, "error": COMPARE_FAILURE_MSG})
            return

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

    # Retry a single failed speaker slot in the current round without advancing.
    if request.retry_speaker_index is not None:
        async with lock:
            turns = await retry_debate_speaker(session, request.retry_speaker_index)
        return debate_response(session, turns)

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
        "label_provider_config": {
            label: {
                "provider": config.provider,
                "official_model_id": config.official_model_id,
                "official_api_base_url": config.official_api_base_url,
                "substitute_chain": list(config.substitute_chain),
            }
            for label, config in LABEL_PROVIDER_CONFIG.items()
        },
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
