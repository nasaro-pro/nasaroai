from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import urllib.parse
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

from custom_agent import CustomWebAgent, _extract_start_url, decide_next_step

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
# OpenRouter free tier exhausted its per-day quota for this account.
DAILY_LIMIT_MSG = (
    "OpenRouter 무료 일일 요청 한도를 모두 사용했습니다. "
    "OpenRouter에 10크레딧을 충전하면 하루 1000회로 늘어나고, "
    "충전 전에는 자정(UTC) 한도 초기화 후 다시 시도해주세요."
)

# Last resort only when OpenRouter's model catalog cannot be fetched at startup.
LAST_RESORT_MODEL = "openai/gpt-4o-mini"

# 에이전트 전용 우선 모델 목록 (작동 확인된 순서)
AGENT_PREFERRED_MODELS = [
    "openai/gpt-4o-mini",
    "openai/gpt-4.1-mini",
    "meta-llama/llama-3.1-8b-instruct:free",
    "meta-llama/llama-4-scout:free",
]

SUMMARY_MODEL_WHITELIST = [
    "openai/gpt-4o-mini",
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
    # allow_origins=["*"] 와 allow_credentials=True 조합은 브라우저가 preflight를
    # 거부한다(쿠키 인증을 안 쓰므로 False가 맞다). 확장/웹 fetch 모두 비인증 요청.
    allow_credentials=False,
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

# 페르소나(개성) 제거: 모든 라벨은 동일한 중립 시스템 메시지를 사용한다.
NEUTRAL_SYSTEM_PROMPT = "한국어로 명확하고 정확하게 답변하세요."

PERSONAS: dict[str, str] = {
    "OpenAI": (
        NEUTRAL_SYSTEM_PROMPT
    ),
    "Anthropic": (
        NEUTRAL_SYSTEM_PROMPT
    ),
    "Google": (
        NEUTRAL_SYSTEM_PROMPT
    ),
    "xAI": (
        NEUTRAL_SYSTEM_PROMPT
    ),
    "Perplexity": (
        NEUTRAL_SYSTEM_PROMPT
    ),
    "DeepSeek": (
        NEUTRAL_SYSTEM_PROMPT
    ),
}

# 토론 발언에 공통으로 덧붙이는 강한 지시: 정보 수집 · 주장 · 비판.
DEBATE_DIRECTIVE = (
    "당신은 치열한 토론의 참가자입니다. 다음을 반드시 지키세요.\n"
    "1) 정보 수집: 주제와 관련된 사실, 데이터, 사례, 통계, 전문가 견해 등 "
    "구체적 근거를 적극적으로 끌어와 제시하세요.\n"
    "2) 주장: 모호하게 양비론으로 빠지지 말고, 명확한 입장을 선택해 강하게 주장하세요.\n"
    "3) 비판: 상대 발언의 논리적 허점, 근거 부족, 반례를 날카롭게 지적하고 반박하세요.\n"
    "감정적 비방은 피하되, 근거에 기반해 단호하고 설득력 있게 한국어로 발언하세요."
)

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


class CollabRecommendRequest(BaseModel):
    task: str


class AgentRequest(BaseModel):
    query: str


class AgentAskRequest(BaseModel):
    query: str
    history: list[dict] = Field(default_factory=list)  # [{mission, result}, ...]


# /agent/step (확장 기반 무상태 두뇌)용 요청 모델.
# [보안] elements에는 비밀번호/카드번호 등 민감 입력값이 들어오면 안 된다.
# 확장(background.js)의 스캔이 input[type="password"] 값은 수집하지 않고
# has_password 플래그만 보낸다. 서버는 만약을 대비해 custom_agent.detect_handoff
# 에서 비밀번호/캡차/결제 폼을 만나면 LLM 호출 없이 즉시 사용자 핸드오프로 정지한다.
class AgentStepRequest(BaseModel):
    task: str
    elements: list[dict] = Field(default_factory=list)
    current_url: str = ""
    action_history: list[dict] = Field(default_factory=list)


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
    round_used_models: list[str] = Field(default_factory=list)


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
COMPARE_SESSION_PLANS: dict[str, dict[str, str]] = {}
COMPARE_SESSION_PENDING: dict[str, int] = {}
COMPARE_SESSION_LOCK = asyncio.Lock()
AGENT_LOCK = asyncio.Semaphore(1)

# /agent/step은 브라우저를 띄우지 않는 순수 LLM 판단이라 부담이 훨씬 적다.
# /agent/task의 AGENT_LOCK(1개)과 공유하지 않고 별도 세마포어로 넉넉히 허용한다.
AGENT_STEP_CONCURRENCY = int(os.environ.get("AGENT_STEP_CONCURRENCY", "8"))
AGENT_STEP_SEMAPHORE = asyncio.Semaphore(AGENT_STEP_CONCURRENCY)


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
                for model_id in get_all_available_free_models():
                    if model_id not in assigned[label] and model_id not in globally_used:
                        assigned[label].append(model_id)
                        globally_used.add(model_id)
                        break
                if (
                    len(assigned[label]) < MIN_MODEL_CANDIDATES_PER_LABEL
                    and LAST_RESORT_MODEL not in assigned[label]
                    and LAST_RESORT_MODEL not in globally_used
                ):
                    assigned[label].append(LAST_RESORT_MODEL)
                    globally_used.add(LAST_RESORT_MODEL)
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


def get_all_available_free_models() -> list[str]:
    if MODEL_CACHE_STATE.all_free_models:
        return list(MODEL_CACHE_STATE.all_free_models)
    pooled = unique(
        model_id
        for candidates in MODEL_CACHE_STATE.free_models_by_label.values()
        for model_id in candidates
    )
    return pooled if pooled else [LAST_RESORT_MODEL]


def build_model_try_order(label: str, excluded: set[str] | None = None) -> list[str]:
    """Ordered unique candidates: label chain first, then global free pool."""
    excluded = excluded or set()
    label_candidates = resolve_label_models(label)
    global_candidates = get_all_available_free_models()
    ordered = unique(label_candidates + [model_id for model_id in global_candidates if model_id not in label_candidates])
    return [model_id for model_id in ordered if model_id not in excluded]


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


def is_daily_free_limit(body_text: str) -> bool:
    """Detect OpenRouter's account-wide per-day free quota exhaustion."""
    lowered = (body_text or "").lower()
    return "free-models-per-day" in lowered or "add 10 credits" in lowered


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
    excluded_models: set[str] | None = None,
) -> ModelCallResult | None:
    """Last resort: try unused free models with a short prompt before giving up."""
    persona = PERSONAS[label]
    excluded = excluded_models or set()
    fallbacks = build_model_try_order(label, excluded)
    if LAST_RESORT_MODEL not in excluded and LAST_RESORT_MODEL not in fallbacks:
        fallbacks.append(LAST_RESORT_MODEL)

    for model_id in fallbacks:
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await request_chat_completion(
                    client, label, model_id, persona, FINAL_ATTEMPT_PROMPT, max_tokens=200
                )
        except httpx.HTTPError as exc:
            logger.warning("Final attempt request failed label=%s model=%s error=%s", label, model_id, exc)
            continue

        if response.status_code != 200:
            logger.warning("Final attempt non-200 label=%s model=%s status=%s", label, model_id, response.status_code)
            continue

        try:
            content = extract_content(response.json())
        except json.JSONDecodeError:
            continue

        if not content.strip():
            continue

        logger.info("Final attempt succeeded label=%s model=%s", label, model_id)
        return ModelCallResult(
            success=True,
            content=content,
            requested_label=label,
            actual_label=label_for_model_id(model_id),
            requested_model=requested_model,
            actual_model=model_id,
            is_real_company_model=model_id.startswith(COMPANY_PREFIXES[label]),
            failed_candidates=failed_candidates,
        )

    return None


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

    candidates = build_model_try_order(label, excluded_models)
    if not candidates:
        return make_failed_result(
            requested_label=label,
            requested_model=LAST_RESORT_MODEL,
            actual_model="",
            failed_candidates=[],
            error=USER_FACING_FAILURE_MSG,
        )

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
                if is_daily_free_limit(response.text):
                    return make_failed_result(
                        requested_label=label,
                        requested_model=requested_model,
                        actual_model=model_id,
                        failed_candidates=failed_candidates,
                        error=DAILY_LIMIT_MSG,
                    )
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
                if is_daily_free_limit(response.text):
                    return make_failed_result(
                        requested_label=label,
                        requested_model=requested_model,
                        actual_model=model_id,
                        failed_candidates=failed_candidates,
                        error=DAILY_LIMIT_MSG,
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

    if excluded_models and LAST_RESORT_MODEL in excluded_models:
        final_result = None
    else:
        final_result = await final_attempt_model_call(
            label, requested_model, failed_candidates, excluded_models=excluded_models
        )
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
            f"{DEBATE_DIRECTIVE}\n\n"
            f"주제: {topic}\n\n"
            "당신은 1번 발언자입니다. 다른 발언자의 내용은 아직 없습니다. "
            "주제에 대해 구체적 근거를 동원해 강한 첫 주장을 한국어로 제시하세요."
        )

    if speaker_index == 1:
        if user_input:
            return (
                f"{DEBATE_DIRECTIVE}\n\n"
                f"지금까지 토론 요약: {previous_summary or '아직 요약이 없습니다.'}\n\n"
                f"사용자가 추가로 다음 질문/의견을 남겼습니다: '{user_input}'\n\n"
                "이 질문/의견을 반드시 반영하여, 근거를 들어 강하게 답변하세요.\n\n"
                f"주제: {topic}"
            )
        return (
            f"{DEBATE_DIRECTIVE}\n\n"
            f"이전 라운드 요약:\n{previous_summary or '아직 요약이 없습니다.'}\n\n"
            f"주제: {topic}\n\n"
            "당신은 새 라운드의 1번 발언자입니다. 이전 라운드 요약을 바탕으로 "
            "반복을 피하고 새로운 근거와 관점으로 강하게 주장하세요."
        )

    role_instruction = "반박/보완" if speaker_index == 2 else "반박하거나 종합"
    # From the point there are multiple prior speeches, compress older ones to keep
    # the prompt within free-model context limits.
    compress_older = len(prior_turns) >= 2
    prior_text = format_prior_turns(prior_turns, compress_older=compress_older)
    return (
        f"{DEBATE_DIRECTIVE}\n\n"
        f"주제: {topic}\n\n"
        f"현재 라운드에서 당신보다 앞선 발언입니다.\n\n"
        f"{prior_text}\n\n"
        f"당신은 {speaker_index}번 발언자입니다. 위 발언의 허점과 약한 근거를 "
        f"구체적으로 짚어 {role_instruction}하고, 당신의 근거를 들어 강하게 답변하세요."
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


def make_failure_turn(
    round_number: int,
    speaker_index: int,
    requested_label: str,
    content: str = USER_FACING_FAILURE_MSG,
) -> DebateTurn:
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
        content=content,
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
    prompt = build_round_prompt(
        topic=session.topic,
        round_number=round_number,
        speaker_index=speaker_index,
        prior_turns=prior_turns,
        previous_summary=session.previous_summary,
        user_input=round_user_input if speaker_index == 1 else None,
    )

    already_used_models: set[str] = set(session.round_used_models)
    already_used_models.update(
        turn.actual_model for turn in session.current_round_turns if turn.actual_model
    )
    already_used_models.update(
        turn.actual_model for turn in prior_turns if turn.actual_model
    )

    excluded = set(already_used_models)
    for _ in range(MAX_MODEL_CANDIDATES_PER_LABEL):
        result = await call_ai_model(
            requested_label,
            prompt,
            excluded_models=excluded,
        )
        session.failed_candidates.extend(result.failed_candidates)
        if not result.success:
            if result.error == DAILY_LIMIT_MSG:
                return make_failure_turn(
                    round_number, speaker_index, requested_label, content=DAILY_LIMIT_MSG
                )
            excluded.update(result.failed_candidates)
            logger.info(
                "Debate speaker failed label=%s speaker=%s error=%s",
                requested_label,
                speaker_index,
                result.error,
            )
            continue
        if result.actual_model in already_used_models:
            logger.warning(
                "Duplicate debate actual_model blocked label=%s speaker=%s model=%s",
                requested_label,
                speaker_index,
                result.actual_model,
            )
            excluded.add(result.actual_model)
            continue
        session.round_used_models.append(result.actual_model)
        return result_to_debate_turn(round_number, speaker_index, requested_label, result)

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
        session.round_used_models = []
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
    session.round_used_models = []
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


async def ensure_compare_session_plan(session_id: str) -> dict[str, str]:
    """Atomically assign one unique primary model per UI label for a compare session."""
    if not session_id:
        return {}

    async with COMPARE_SESSION_LOCK:
        existing = COMPARE_SESSION_PLANS.get(session_id)
        if existing is not None:
            return existing

        used: set[str] = set()
        plan: dict[str, str] = {}

        for label in COMPANY_LABELS:
            for model_id in resolve_label_models(label):
                if model_id not in used:
                    plan[label] = model_id
                    used.add(model_id)
                    break

        for label in COMPANY_LABELS:
            if label in plan:
                continue
            for model_id in get_all_available_free_models():
                if model_id not in used:
                    plan[label] = model_id
                    used.add(model_id)
                    break

        COMPARE_SESSION_PLANS[session_id] = plan
        logger.info("Compare session plan session_id=%s plan=%s", session_id[:8], plan)
        return plan


def build_compare_candidate_pool(
    label: str,
    plan: dict[str, str],
    excluded_by_failure: set[str],
) -> list[str]:
    """Label plan primary first, then label chain, then global free models."""
    ordered: list[str] = []
    if label in plan:
        ordered.append(plan[label])
    ordered.extend(resolve_label_models(label))
    ordered.extend(get_all_available_free_models())
    pool = unique(ordered)
    return [model_id for model_id in pool if model_id not in excluded_by_failure]


async def acquire_compare_model(
    session_id: str,
    label: str,
    excluded_by_failure: set[str],
) -> str | None:
    """
    Pick the next unused actual_model for this label within a compare session.
    Falls back to the global free pool when label candidates are exhausted.
    """
    if not session_id:
        pool = build_model_try_order(label, excluded_by_failure)
        return pool[0] if pool else None

    plan = await ensure_compare_session_plan(session_id)
    pool = build_compare_candidate_pool(label, plan, excluded_by_failure)
    if not pool:
        return None

    async with COMPARE_SESSION_LOCK:
        used = COMPARE_ACTIVE_MODELS.setdefault(session_id, set())
        for model_id in pool:
            if model_id not in used:
                used.add(model_id)
                return model_id
        logger.warning(
            "Compare model pool exhausted session_id=%s label=%s used=%s pool=%s",
            session_id[:8],
            label,
            sorted(used),
            pool,
        )
        return None


async def mark_compare_stream_started(session_id: str) -> None:
    if not session_id:
        return
    await ensure_compare_session_plan(session_id)
    async with COMPARE_SESSION_LOCK:
        COMPARE_SESSION_PENDING[session_id] = COMPARE_SESSION_PENDING.get(session_id, 0) + 1


async def mark_compare_stream_done(session_id: str) -> None:
    if not session_id:
        return
    async with COMPARE_SESSION_LOCK:
        pending = COMPARE_SESSION_PENDING.get(session_id)
        if pending is None:
            return
        pending -= 1
        if pending <= 0:
            COMPARE_SESSION_PLANS.pop(session_id, None)
            COMPARE_ACTIVE_MODELS.pop(session_id, None)
            COMPARE_SESSION_PENDING.pop(session_id, None)
        else:
            COMPARE_SESSION_PENDING[session_id] = pending


async def release_compare_model(session_id: str, model_id: str) -> None:
    if not session_id or not model_id:
        return
    async with COMPARE_SESSION_LOCK:
        bucket = COMPARE_ACTIVE_MODELS.get(session_id)
        if bucket is not None:
            bucket.discard(model_id)
            if not bucket and session_id not in COMPARE_SESSION_PENDING:
                COMPARE_ACTIVE_MODELS.pop(session_id, None)


@app.post("/compare/stream")
async def stream_compare(data: CompareRequest) -> StreamingResponse:
    if data.model_name not in COMPANY_LABELS:
        raise HTTPException(status_code=400, detail="Invalid model_name")

    await ensure_model_cache_fresh()
    label = data.model_name
    persona = PERSONAS[label]
    session_id = data.compare_session_id.strip()

    async def generate() -> AsyncIterator[str]:
        excluded_by_failure: set[str] = set()
        await mark_compare_stream_started(session_id)
        current_model_id: str | None = None

        try:
            while True:
                # 직전 모델(실패해서 다음 후보로 넘어가는 경우)을 먼저 반납해 누수를 막는다.
                if current_model_id:
                    await release_compare_model(session_id, current_model_id)
                    current_model_id = None

                current_model_id = await acquire_compare_model(session_id, label, excluded_by_failure)
                model_id = current_model_id
                if model_id is None:
                    yield sse({"model": data.model_name, "success": False, "error": COMPARE_FAILURE_MSG})
                    return

                failed_this_model = False
                try:
                    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                        for attempt in range(2):
                            async with client.stream(
                                "POST",
                                OPENROUTER_CHAT_URL,
                                headers=build_openrouter_headers(),
                                json={
                                    **build_chat_payload(
                                        model_id=model_id,
                                        persona=persona,
                                        prompt=data.message,
                                        stream=True,
                                    )
                                },
                            ) as response:
                                if response.status_code == 429:
                                    body = (await response.aread()).decode("utf-8", "ignore")
                                    if is_daily_free_limit(body):
                                        yield sse({"model": data.model_name, "success": False, "error": DAILY_LIMIT_MSG})
                                        return
                                    if attempt == 0:
                                        await asyncio.sleep(RATE_LIMIT_RETRY_DELAY_SECONDS)
                                        continue
                                    excluded_by_failure.add(model_id)
                                    failed_this_model = True
                                    break

                                if response.status_code != 200:
                                    body = (await response.aread()).decode("utf-8", "ignore")
                                    logger.warning(
                                        "Stream non-200 label=%s model=%s status=%s",
                                        label,
                                        model_id,
                                        response.status_code,
                                    )
                                    if is_daily_free_limit(body):
                                        yield sse({"model": data.model_name, "success": False, "error": DAILY_LIMIT_MSG})
                                        return
                                    if should_try_next_candidate(response.status_code):
                                        excluded_by_failure.add(model_id)
                                        failed_this_model = True
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
                    logger.warning("Stream HTTPError label=%s model=%s error=%s", label, model_id, exc)
                    excluded_by_failure.add(model_id)
                    failed_this_model = True
                except Exception:
                    logger.exception("Unexpected stream error label=%s model=%s", label, model_id)
                    yield sse({"model": data.model_name, "success": False, "error": COMPARE_FAILURE_MSG})
                    return

                if not failed_this_model:
                    return
        finally:
            # 스트림이 정상 종료/예외/early return 어느 경로로 끝나도 모델 락을 반드시 반납한다.
            if current_model_id:
                await release_compare_model(session_id, current_model_id)
            await mark_compare_stream_done(session_id)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/collab/recommend")
async def collab_recommend(data: CollabRecommendRequest) -> dict:
    """사용자 작업 설명을 받아 OpenAI label로 각 단계별 추천 AI 목록을 반환한다."""
    await ensure_model_cache_fresh()

    prompt = (
        f"사용자가 원하는 작업: {data.task}\n\n"
        "아래 4단계에 맞춰 각 단계에서 가장 적합한 AI 도구를 2~3개 추천하고, "
        "각각 한 줄 이유를 달아주세요. 한국어로 답변하세요.\n\n"
        "1단계 — 정보 조사 및 아이디어 생성\n"
        "2단계 — 논리구조 생성 및 프롬프트 지시\n"
        "3단계 — 작업물 생성\n"
        "4단계 — 오류 검증 및 업그레이드"
    )

    result = await call_ai_model("OpenAI", prompt, max_tokens=600)
    return {
        "recommendation": result.content if result.success else "추천 생성에 실패했습니다. 다시 시도해주세요.",
        "success": result.success,
    }


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


AGENT_STEP_MAX_CANDIDATES = 4


def _resolve_agent_models() -> list[str]:
    """에이전트가 시도할 모델 목록(우선순위 폴백 체인).

    1순위: AGENT_MODEL 환경변수 (설정된 경우)
    2순위: AGENT_PREFERRED_MODELS (gpt-4o-mini 등 확인된 모델)
    3순위: OpenRouter에서 가져온 무료 모델 목록
    """
    configured = os.environ.get("AGENT_MODEL", "").strip()
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    # 작동 확인된 선호 모델 먼저 추가
    candidates.extend(AGENT_PREFERRED_MODELS)
    # 추가로 무료 모델도 폴백으로
    candidates.extend(get_all_available_free_models())
    deduped = list(dict.fromkeys(c for c in candidates if c))
    if not deduped:
        deduped = [LAST_RESORT_MODEL]
    return deduped[:AGENT_STEP_MAX_CANDIDATES]


@app.post("/agent/step")
async def agent_step(request: AgentStepRequest):
    """확장 프로그램이 사용자의 실제 탭에서 화면을 스캔해 보내면, 다음에 할
    액션 1개만 판단해 돌려주는 무상태 엔드포인트. Playwright를 전혀 쓰지 않는다.
    실제 클릭/입력은 확장(background.js)이 chrome.debugger로 수행한다."""
    task = request.task.strip()
    if not task:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "작업 내용을 입력해주세요."},
        )

    await ensure_model_cache_fresh()
    models = _resolve_agent_models()
    headers = build_openrouter_headers()

    async with AGENT_STEP_SEMAPHORE:
        try:
            return await decide_next_step(
                headers=headers,
                models=models,
                task=task,
                elements=request.elements,
                action_history=request.action_history,
                current_url=request.current_url,
            )
        except Exception:
            logger.exception("agent_step failed task=%s", task[:120])
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": "에이전트 판단 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."},
            )


@app.post("/agent/task")
async def agent_task(request: AgentRequest):
    query = request.query.strip()
    if not query:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "작업 내용을 입력해주세요."},
        )

    if AGENT_LOCK.locked():
        return JSONResponse(
            status_code=429,
            content={"status": "error", "message": "이미 다른 에이전트 작업이 실행 중입니다. 잠시 후 다시 시도해주세요."},
        )

    # 유료 호출 차단: 무료 모델만 사용한다(402 Insufficient credits 방지).
    await ensure_model_cache_fresh()
    model = _resolve_agent_models()[0]
    headless = os.environ.get("AGENT_HEADLESS", "true").lower() != "false"
    headers = build_openrouter_headers()
    start_url = _extract_start_url(query)

    async with AGENT_LOCK:
        try:
            async with async_playwright() as p:
                browserless_token = os.environ.get("BROWSERLESS_TOKEN", "")
                browserless_endpoint = os.environ.get(
                    "BROWSERLESS_ENDPOINT",
                    f"wss://chrome.browserless.io?token={browserless_token}",
                )

                if browserless_token:
                    try:
                        browser = await p.chromium.connect_over_cdp(
                            browserless_endpoint,
                            timeout=20000,
                        )
                    except Exception as cdp_err:
                        logger.error("[Agent] Browserless 연결 실패 (token 미노출)")
                        raise RuntimeError(
                            "브라우저 연결에 실패했습니다. BROWSERLESS_TOKEN을 확인해주세요."
                        ) from cdp_err
                else:
                    try:
                        browser = await p.chromium.launch(
                            headless=headless,
                            args=["--no-sandbox", "--disable-dev-shm-usage"],
                        )
                    except Exception as launch_err:
                        logger.error("[Agent] 로컬 Chromium 실행 실패: %s", launch_err)
                        raise RuntimeError(
                            "브라우저를 실행하지 못했습니다. 서버에 BROWSERLESS_TOKEN을 설정하거나 "
                            "로컬에서 'playwright install chromium'을 실행해주세요."
                        ) from launch_err
                try:
                    context = await browser.new_context(
                        viewport={"width": 1280, "height": 800},
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                    )
                    try:
                        page = await context.new_page()
                        await page.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                        agent = CustomWebAgent(openrouter_headers=headers, model=model)
                        result = await agent.run(query, page)
                        return {"status": "success", "result": result}
                    finally:
                        await context.close()
                finally:
                    await browser.close()
        except RuntimeError as setup_err:
            logger.warning("CustomWebAgent setup failed: %s", setup_err)
            return JSONResponse(
                status_code=503,
                content={"status": "error", "message": str(setup_err)},
            )
        except Exception:
            logger.exception("CustomWebAgent failed query=%s", query[:120])
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": "브라우저 에이전트 작업 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."},
            )


@app.post("/agent/ask")
async def agent_ask(request: AgentAskRequest):
    """브라우저 없이 순수 LLM으로 임무를 수행한다.
    /agent/task(Playwright)가 실패할 때의 폴백이자, 단순 분석/답변 임무에 사용."""
    query = request.query.strip()
    if not query:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "임무를 입력해주세요."},
        )

    await ensure_model_cache_fresh()
    models = _resolve_agent_models()
    headers = build_openrouter_headers()

    # 이전 임무 기록을 컨텍스트로 추가
    history_ctx = ""
    for item in (request.history or [])[-3:]:
        m = item.get("mission", "").strip()
        r = item.get("result", "").strip()
        if m and r:
            history_ctx += f"\n[이전 임무] {m}\n[결과 요약] {r[:300]}\n"

    system_prompt = (
        "당신은 Nasaro AI 에이전트입니다. 사용자가 지시한 임무를 분석하고 "
        "최선의 결과를 한국어로 상세하게 제공합니다. "
        "웹 자동화가 필요한 임무는 일반 지식과 추론으로 최대한 지원하고, "
        "다음에 취해야 할 행동이나 확인 방법을 구체적으로 안내합니다. "
        "응답은 충분히 자세하고 실용적이어야 합니다."
    )
    user_msg = (
        f"{history_ctx}\n[현재 임무]\n{query}" if history_ctx
        else f"[임무]\n{query}"
    )

    last_err = "AI 응답을 받지 못했습니다."
    for model in models[:5]:
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    OPENROUTER_CHAT_URL,
                    headers=headers,
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user",   "content": user_msg},
                        ],
                        "max_tokens": 1200,
                    },
                )
            if resp.status_code == 429:
                last_err = "요청 한도 초과. 잠시 후 다시 시도해주세요."
                continue
            if resp.status_code != 200:
                last_err = f"AI 서버 오류 ({resp.status_code})"
                continue
            data = resp.json()
            result = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            if result:
                return {"status": "success", "result": result, "mode": "llm"}
        except Exception as e:
            last_err = str(e)
            continue

    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": last_err},
    )


@app.get("/")
def serve_home() -> FileResponse:
    index_path = os.path.join(BASE_DIR, "index.html")
    return FileResponse(index_path)


@app.get("/install")
def serve_install() -> FileResponse:
    return FileResponse(os.path.join(BASE_DIR, "install.html"))


@app.get("/manifest.json")
def serve_manifest() -> FileResponse:
    path = os.path.join(BASE_DIR, "manifest.json")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/manifest+json")


@app.get("/extension-update")
def extension_update(request: Request):
    """Chrome 확장 자동 업데이트 매니페스트 (update_url)"""
    # 브라우저가 쿼리 파라미터로 ?x=id%3D{id}%26v%3D{ver}%26uc 형태로 보냄
    raw = request.query_params.get("x", "")
    ext_id = ""
    for part in raw.replace("%3D", "=").replace("%26", "&").split("&"):
        if part.startswith("id="):
            ext_id = part[3:]
    if not ext_id:
        ext_id = "arenax-agent"
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>"
        f"<app appid='{ext_id}'>"
        "<updatecheck"
        " status='ok'"
        " version='2.2.0'"
        " prodversionmin='88.0'"
        " codebase='https://arenax-4812.onrender.com/static/arenax-extension.zip'"
        "/>"
        "</app>"
        "</gupdate>"
    )
    from fastapi.responses import Response as FastResponse
    return FastResponse(content=xml, media_type="application/xml")


@app.get("/static/{filename}")
def serve_static(filename: str):
    safe = os.path.basename(filename)
    path = os.path.join(BASE_DIR, "static", safe)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")
    if safe.endswith(".zip"):
        return FileResponse(
            path,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{safe}"'},
        )
    return FileResponse(path)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
