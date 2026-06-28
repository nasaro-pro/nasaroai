from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

from custom_agent import CustomWebAgent, _extract_start_url, decide_next_step
from db_cloud_sync import cloud_backup_enabled, restore_db_from_cloud, upload_db_if_changed
from auth_store import (
    add_support_reply,
    admin_adjust_quota,
    admin_set_quota_limit,
    admin_logout,
    check_and_consume_quota,
    count_activity_log,
    create_admin_session,
    create_public_share,
    create_support_inquiry,
    db_connection,
    delete_support_inquiry,
    delete_support_inquiry_admin,
    delete_activity_records,
    get_activity_by_id,
    get_activity_retention_days,
    get_admin_setting,
    get_activity_log,
    get_admin_dashboard,
    get_public_share,
    get_quota_snapshot,
    get_support_thread,
    get_user_admin_detail,
    get_user_by_token,
    get_user_data,
    init_db,
    DB_PATH,
    is_guest_subject,
    is_subject_banned,
    list_guest_devices,
    list_support_inquiries,
    list_user_support_inquiries,
    log_activity,
    log_user_activity_detail,
    login as auth_login_fn,
    logout as auth_logout_fn,
    merge_user_data,
    purge_expired_activity,
    resolve_device_id,
    search_users_admin,
    set_subject_ban,
    set_admin_setting,
    touch_device_presence,
    signup as auth_signup_fn,
    verify_admin_password,
    verify_admin_token,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("nasaroai")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
COMPARE_STREAM_TIMEOUT = httpx.Timeout(35.0, connect=10.0)
MODEL_REFRESH_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_COMPARE_STREAM_MODEL_ATTEMPTS = 5
MODEL_CACHE_TTL_SECONDS = 600
# Allow up to 10 candidates per label so a label can keep falling back to other
# models even when several are rate-limited or out of context budget.
MAX_MODEL_CANDIDATES_PER_LABEL = 10
MIN_MODEL_CANDIDATES_PER_LABEL = 3
HEALTHCHECK_CONCURRENCY = 2
HEALTHCHECK_DELAY_SECONDS = 1.0
LABEL_HEALTH_TTL_SECONDS = 600

LABEL_HEALTH: dict[str, dict] = {}
LABEL_HEALTH_REFRESHED_AT = 0.0
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
LAST_RESORT_MODEL = "openai/gpt-oss-20b:free"

# 에이전트 폴백용 무료 모델 목록 (유료 primary 실패 시 사용)
AGENT_PREFERRED_MODELS = [
    "openai/gpt-oss-20b:free",
    "openai/gpt-oss-120b:free",
    "meta-llama/llama-3.1-8b-instruct:free",
    "meta-llama/llama-4-scout:free",
]

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

app = FastAPI(title="Nasaro AI Backend")


@app.on_event("startup")
async def _startup_db() -> None:
    init_db()
    logger.info("Nasaro DB path: %s (cloud_backup=%s)", DB_PATH, cloud_backup_enabled())
    if cloud_backup_enabled():
        asyncio.create_task(_cloud_db_bootstrap())


async def _cloud_db_bootstrap() -> None:
    try:
        restored = await asyncio.to_thread(restore_db_from_cloud, DB_PATH)
        if restored:
            await asyncio.to_thread(init_db)
        await asyncio.to_thread(upload_db_if_changed, DB_PATH)
    except Exception:
        logger.exception("Cloud DB bootstrap failed")


async def _periodic_db_cloud_backup() -> None:
    while True:
        await asyncio.sleep(90)
        upload_db_if_changed(DB_PATH)


@app.on_event("startup")
async def _start_db_backup_loop() -> None:
    if cloud_backup_enabled():
        asyncio.create_task(_periodic_db_cloud_backup())


@app.on_event("startup")
async def _start_self_ping_loop() -> None:
    try:
        asyncio.create_task(_self_ping_loop())
    except Exception:
        pass


async def _self_ping_loop() -> None:
    try:
        await asyncio.sleep(60)
        self_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
        if not self_url:
            return
        while True:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.get(f"{self_url}/health")
            except Exception:
                pass
            await asyncio.sleep(840)
    except Exception:
        pass


# 협업 단계별 역할 AI — 조사/구조/제작/검증을 서로 다른 모델이 담당
COLLAB_STAGE_MODEL_LABELS = ["Perplexity", "Anthropic", "OpenAI", "DeepSeek"]

COLLAB_QUICK_TEMPLATES = [
    {"title": "유튜브 숏츠", "task": "30초 유튜브 숏츠 영상 기획부터 업로드까지"},
    {"title": "기획 보고서", "task": "신규 서비스 기획 보고서 작성"},
    {"title": "랜딩페이지", "task": "스타트업 랜딩페이지 기획 및 제작"},
    {"title": "앱 MVP", "task": "모바일 앱 MVP 기능 개발 및 배포"},
    {"title": "PPT 발표", "task": "투자 IR 10분 발표용 PPT 제작"},
    {"title": "마케팅 카피", "task": "신제품 런칭 광고 카피와 랜딩 문구 작성"},
]


def _bearer_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _resolve_subject(request: Request, device_id: str | None = None) -> tuple[str, dict | None]:
    user = get_user_by_token(_bearer_token(request))
    if user:
        return f"user:{user['id']}", user
    dev = (device_id or request.headers.get("X-Device-Id") or "anonymous").strip()
    return f"device:{dev}", None


def _platform(request: Request) -> str:
    p = (request.headers.get("X-Platform") or "web").strip().lower()
    if p in ("app", "mobile_web", "web"):
        return p
    return "web"


def _quota_error_payload(feature: str, info: dict) -> dict:
    feat_label = {"compare": "비교", "debate": "토론", "collab": "협업", "agent": "에이전트"}.get(feature, feature)
    pool_limit = int(info.get("limit", 0))
    pool_used = int(info.get("used", 0))
    if info.get("banned"):
        return {
            "message": "이 계정 또는 기기는 사용이 제한되었습니다. 문의해 주세요.",
            "quota": info,
            "banned": True,
        }
    if info.get("guest"):
        return {
            "message": (
                f"오늘 비회원 코인을 모두 사용했습니다 ({pool_used}/{pool_limit}🪙). "
                "로그인하면 하루 250🪙까지 이용할 수 있습니다."
            ),
            "quota": info,
            "login_required": True,
        }
    return {
        "message": (
            f"오늘 코인을 모두 사용했습니다 ({pool_used}/{pool_limit}🪙). "
            "내일 자정(KST)에 초기화됩니다."
        ),
        "quota": info,
    }


def _require_quota(
    request: Request,
    feature: str,
    device_id: str | None = None,
    *,
    action: str = "",
    detail: str = "",
    amount: float = 1.0,
) -> dict | None:
    subject, user = _resolve_subject(request, device_id)
    if is_subject_banned(subject):
        limits = get_quota_snapshot(subject)["limits"]
        info = {
            "feature": feature,
            "used": 0,
            "limit": limits.get(feature, 0),
            "banned": True,
            "guest": is_guest_subject(subject),
        }
        raise HTTPException(status_code=403, detail=_quota_error_payload(feature, info))
    ok, info = check_and_consume_quota(subject, feature, amount=amount)
    if ok:
        dev = (device_id or request.headers.get("X-Device-Id") or "").strip()
        log_activity(
            subject,
            feature,
            user_id=user["id"] if user else None,
            device_id=dev,
            platform=_platform(request),
            action=action or feature,
            detail=(detail or "")[:500],
        )
        return user
    raise HTTPException(
        status_code=429,
        detail=_quota_error_payload(feature, info),
    )


def _compare_quota_key(subject: str, session_id: str) -> str:
    sid = session_id.strip()
    return f"{subject}:{sid}" if sid else subject


async def _require_compare_coin(
    request: Request,
    device_id: str | None = None,
) -> dict | None:
    """Charge 1 coin per compare model stream (1 AI call = 1 coin)."""
    subject, user = _resolve_subject(request, device_id)
    ok, info = await asyncio.to_thread(check_and_consume_quota, subject, "compare", 1.0)
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=_quota_error_payload("compare", info),
        )
    dev = (device_id or request.headers.get("X-Device-Id") or "").strip()
    log_activity(
        subject,
        "compare",
        user_id=user["id"] if user else None,
        device_id=dev,
        platform=_platform(request),
        action="compare",
        detail="compare AI call",
    )
    return user


AGENT_MISSION_CHARGED: set[str] = set()
AGENT_MISSION_LOCK = asyncio.Lock()


def _agent_mission_key(subject: str, mission_id: str) -> str:
    mid = (mission_id or "").strip()
    return f"{subject}:{mid}" if mid else subject


async def _require_agent_coin(
    request: Request,
    device_id: str | None = None,
    *,
    action: str = "agent",
    detail: str = "",
) -> dict | None:
    """Charge 1 coin per agent AI call."""
    subject, user = _resolve_subject(request, device_id)
    ok, info = await asyncio.to_thread(check_and_consume_quota, subject, "agent", 1.0)
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=_quota_error_payload("agent", info),
        )
    dev = (device_id or request.headers.get("X-Device-Id") or "").strip()
    log_activity(
        subject,
        "agent",
        user_id=user["id"] if user else None,
        device_id=dev,
        platform=_platform(request),
        action=action,
        detail=(detail or action)[:500],
    )
    return user


async def _require_debate_coin(
    request: Request,
    device_id: str | None = None,
) -> dict | None:
    """Charge 1 coin per debate AI speaker call."""
    subject, user = _resolve_subject(request, device_id)
    ok, info = await asyncio.to_thread(check_and_consume_quota, subject, "debate", 1.0)
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=_quota_error_payload("debate", info),
        )
    dev = (device_id or request.headers.get("X-Device-Id") or "").strip()
    log_activity(
        subject,
        "debate",
        user_id=user["id"] if user else None,
        device_id=dev,
        platform=_platform(request),
        action="debate",
        detail="debate AI call",
    )
    return user


def _quota_error_message(detail: object) -> str:
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message.strip():
            return message
    if isinstance(detail, str) and detail.strip():
        return detail
    return "오늘 compare 사용 한도를 모두 사용했습니다."

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

# 토론: 형식 토론이 아니라 주제/질문에 각자 답·비판·주장
DEBATE_CORE = (
    "아래 '주제'는 사용자의 질문입니다. 형식적인 토론·역할극·절차 안내가 아닙니다.\n"
    "토론 규칙 설명, '이제 시작합니다', '양측 의견을 정리하면' 같은 메타 발언 금지.\n"
    "주제에서 벗어난 추상론·헛소리·뜬구름 잡기 금지. 질문/주제에 직접 답하세요.\n"
    "근거(사실·수치·구체 사례)를 포함하고 한국어로 작성하세요."
)

DEBATE_DIRECTIVE = (
    f"{DEBATE_CORE}\n"
    "앞선 발언자들은 같은 주제에 대해 각자 답한 것입니다.\n"
    "1) 앞선 답에서 타당한 점은 인정하세요.\n"
    "2) 틀리거나 빠진 점, 약한 근거는 주제와 연결해 구체적으로 비판·보완하세요.\n"
    "3) 그다음 자신이 이 주제에 대해 어떻게 답하는지(주장+근거)를 분명히 제시하세요.\n"
    "무조건 반대만 하지 말고, 질문에 대한 실질적 답을 포함하세요."
)

# 1번 발언자: 주제에 대한 자기 답만
SPEAKER1_DIRECTIVE = (
    f"{DEBATE_CORE}\n"
    "당신은 1번 발언자입니다. 주제(질문)에 대한 자신의 답 — 핵심 주장 1개와 "
    "근거(사실·수치·사례 2~3개)만 작성하세요.\n"
    "다른 발언자 입장을 대신 말하거나, 찬반 양쪽을 정리·종합하지 마세요.\n"
    "앞선 발언에 대한 반박은 없습니다(첫 발언). 3~6문장, 200~400자."
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


# Paid primary models via OpenRouter; substitute_chain is free-model fallback on failure.
LABEL_PROVIDER_CONFIG: dict[str, LabelProviderConfig] = {
    "OpenAI": LabelProviderConfig(
        label="OpenAI",
        official_model_id="openai/gpt-4o-mini",
        substitute_chain=(
            "openai/gpt-5.5",
            "~openai/gpt-mini-latest",
            "openai/gpt-chat-latest",
        ),
    ),
    "Anthropic": LabelProviderConfig(
        label="Anthropic",
        official_model_id="anthropic/claude-3-haiku",
        substitute_chain=(
            "~anthropic/claude-haiku-latest",
            "~anthropic/claude-sonnet-latest",
            "anthropic/claude-opus-4.8-fast",
        ),
    ),
    "Google": LabelProviderConfig(
        label="Google",
        official_model_id="google/gemini-3.1-flash-lite",
        substitute_chain=(
            "~google/gemini-flash-latest",
            "google/gemini-3.5-flash",
            "~google/gemini-pro-latest",
        ),
    ),
    "xAI": LabelProviderConfig(
        label="xAI",
        official_model_id="x-ai/grok-4.3",
        substitute_chain=(
            "x-ai/grok-build-0.1",
        ),
    ),
    "Perplexity": LabelProviderConfig(
        label="Perplexity",
        official_model_id="perplexity/sonar",
        substitute_chain=(
            "perplexity/sonar-pro",
            "perplexity/sonar-reasoning",
        ),
    ),
    "DeepSeek": LabelProviderConfig(
        label="DeepSeek",
        official_model_id="deepseek/deepseek-chat",
        substitute_chain=(
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4-pro",
        ),
    ),
}


@dataclass
class ModelCacheState:
    loaded: bool = False
    source: str = "not_loaded"
    error: str | None = None
    refreshed_at: float = 0.0
    all_model_ids: set[str] = field(default_factory=set)
    free_model_ids: set[str] = field(default_factory=set)
    free_models_by_label: dict[str, list[str]] = field(default_factory=dict)
    all_free_models: list[str] = field(default_factory=list)


MODEL_CACHE_STATE = ModelCacheState()
MODEL_CACHE_LOCK = asyncio.Lock()


class CompareRequest(BaseModel):
    message: str
    model_name: str
    compare_session_id: str = ""
    user_id: str = ""


class CollabIntakeMessage(BaseModel):
    role: str
    content: str


class CollabIntakeRequest(BaseModel):
    task: str
    messages: list[CollabIntakeMessage] = Field(default_factory=list)
    user_id: str = ""
    intake_model: str = "Claude"


class AdminQuotaLimitRequest(BaseModel):
    subject: str
    feature: str
    daily_limit: float


class CollabRecommendRequest(BaseModel):
    task: str
    user_id: str = ""


class CollabStageRequest(BaseModel):
    task: str
    work_type: str
    stage_index: int
    stage_name: str
    actions: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    previous_notes: str = ""
    acceptance: list[str] = Field(default_factory=list)
    stage_model: str = ""
    user_id: str = ""
    verification_feedback: str = ""
    is_rework: bool = False
    artifact_under_review: str = ""


class CollabFollowupRequest(BaseModel):
    task: str
    original_task: str = ""
    work_type: str = ""
    stage_outputs: list[str] = Field(default_factory=list)
    user_id: str = ""
    pre_work: bool = False


class AuthSignupRequest(BaseModel):
    username: str
    email: str
    password: str


class AuthLoginRequest(BaseModel):
    username: str
    password: str


class UserSyncRequest(BaseModel):
    compare_history: list[dict] | None = None
    collab_plans: list[dict] | None = None
    agent_timeline: list[dict] | None = None
    active_collab: dict | None = None
    saved_works: list[dict] | None = None
    extension_prefs: dict | None = None
    ai_presets: list[dict] | None = None
    ai_settings: dict | None = None
    session_history: list[dict] | None = None


class CompareSummaryRequest(BaseModel):
    message: str
    responses: dict[str, str] = Field(default_factory=dict)
    user_id: str = ""


class DebateRoundSummaryRequest(BaseModel):
    topic: str
    round_number: int = 1
    turns: list[dict] = Field(default_factory=list)
    user_id: str = ""


class ShareCreateRequest(BaseModel):
    kind: str = "compare"
    title: str = ""
    payload: dict = Field(default_factory=dict)


class AdminLoginRequest(BaseModel):
    password: str


class SupportInquiryRequest(BaseModel):
    message: str
    device_id: str = ""


class SupportReplyRequest(BaseModel):
    message: str


class PresenceRequest(BaseModel):
    device_id: str | None = None


class DeviceRegisterRequest(BaseModel):
    fingerprint: str = ""
    device_id: str = ""


class AdminQuotaAdjustRequest(BaseModel):
    subject: str
    feature: str
    delta: float = 0.0


class AdminBanRequest(BaseModel):
    subject: str
    banned: bool = True
    reason: str = ""


class UserActivityLogRequest(BaseModel):
    feature: str
    action: str = ""
    question: str = ""
    answer: str = ""
    device_id: str | None = None
    privacy: bool = False


class AdminActivityDeleteRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)
    all: bool = False


class AdminSettingsRequest(BaseModel):
    activity_retention_days: int = 0


def _privacy_from_request(request: Request, user: dict | None, body_privacy: bool = False) -> bool:
    if not user:
        return False
    header = (request.headers.get("X-Privacy-Mode") or "").strip().lower()
    header_on = header in ("1", "true", "yes")
    return bool(body_privacy or header_on)


def _admin_bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _require_admin(request: Request) -> str:
    token = _admin_bearer(request)
    if not token or not verify_admin_token(token):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")
    return token


COLLAB_TYPE_RULES: list[dict] = [
    {
        "type": "자기소개서·취업",
        "keywords": [
            "자기소개서", "자소서", "자기소개", "입사", "취업", "지원서", "cover letter",
            "resume", "cv", "면접", "경력기술서", "포트폴리오", "jd", "채용",
        ],
        "tools": {
            "research": ["Perplexity", "LinkedIn", "잡코리아/원티드 JD"],
            "structure": ["Claude", "ChatGPT"],
            "production": ["Claude", "ChatGPT", "Notion AI"],
            "review": ["DeepSeek", "Claude"],
        },
        "algorithm": [
            "지원 직무·회사/학교, 강조할 경력·역량, 분량·형식(글자수/항목)을 확정한다.",
            "JD(직무기술서)와 본인 경력의 매칭 포인트, 성과 수치, 차별화 근거를 조사한다.",
            "도입→경력→역량→지원동기→마무리 흐름과 문단별 핵심 메시지·키워드를 설계한다.",
            "직무 맞춤형 자기소개서·지원서 초안을 작성한다(항목별 분리).",
            "과장·누락·직무 불일치·맞춤법·톤·분량·중복을 검증한다. 직접 재작성하지 않고 오류·보완점과 수정 지시만 작성한다.",
            "조사·구조 AI에게 피드백 기반 재조사·재설계 지시를 전달하고, 제작 AI는 수정된 구조로 초안을 갱신한다.",
            "통과 기준(직무 적합·구체적 성과·일관된 톤·분량 준수)을 만족하면 최종본을 확정한다.",
        ],
        "acceptance": ["직무와 내용 일치", "성과·수치 구체적", "항목별 분량 준수", "과장·누락 없음"],
    },
    {
        "type": "동영상 제작",
        "keywords": ["동영상", "영상", "숏츠", "유튜브", "릴스", "틱톡", "편집", "자막", "나레이션"],
        "tools": {
            "research": ["Perplexity", "YouTube 트렌드", "Google Trends"],
            "structure": ["Claude", "ChatGPT", "Whimsical AI"],
            "production": ["CapCut", "Vrew", "HeyGen", "ElevenLabs", "Suno"],
            "review": ["Claude", "Grammarly", "YouTube Studio 분석"],
        },
        "algorithm": [
            "시청자, 플랫폼, 길이, 핵심 메시지를 먼저 확정한다.",
            "상위 5개 유사 콘텐츠를 조사해 훅/전개/CTA 패턴을 추출한다.",
            "3초 훅, 장면별 스크립트, 자막 문장, 컷 전환표를 만든다.",
            "영상 생성/편집 도구로 1차본을 만들고 음성·자막·BGM을 붙인다.",
            "무음 시청 가독성, 첫 3초 이탈, 저작권, CTA 명확성을 검증한다.",
            "검증 실패 항목을 장면 단위로 재제작하고 다시 검사한다.",
            "썸네일/제목/설명/태그까지 패키지화하면 통과 처리한다.",
        ],
        "acceptance": ["첫 3초 안에 주제가 보임", "자막만 봐도 이해됨", "저작권 위험 없음", "CTA가 명확함"],
    },
    {
        "type": "문서 제작",
        "keywords": ["문서", "보고서", "제안서", "기획서", "논문", "정리", "요약", "계약서", "레포트", "원고"],
        "tools": {
            "research": ["Perplexity", "NotebookLM", "ChatPDF"],
            "structure": ["Claude", "ChatGPT", "Notion AI"],
            "production": ["Notion AI", "Google Docs", "DeepL"],
            "review": ["Grammarly", "QuillBot", "Claude"],
        },
        "algorithm": [
            "문서 목적, 독자, 분량, 필수 포함 항목을 정의한다.",
            "근거 자료를 수집하고 출처별 신뢰도를 표시한다.",
            "목차, 핵심 주장, 근거, 예시, 결론 구조를 만든다.",
            "초안을 작성하고 문단별 역할이 겹치지 않게 정리한다.",
            "사실 오류, 논리 비약, 문체, 중복, 출처 누락을 검증한다.",
            "검증 실패 문단을 재작성하고 다시 교정한다.",
            "요약본, 원문, 체크리스트를 함께 제공하면 통과 처리한다.",
        ],
        "acceptance": ["목적과 독자가 명확함", "근거 출처가 있음", "목차 흐름이 자연스러움", "중복 문단이 없음"],
    },
    {
        "type": "앱·웹 개발",
        "keywords": ["앱", "웹", "사이트", "개발", "코드", "프로그램", "기능", "버그", "배포", "api"],
        "tools": {
            "research": ["Docs", "Stack Overflow", "GitHub"],
            "structure": ["Claude", "ChatGPT", "Mermaid"],
            "production": ["Cursor", "GitHub Copilot", "Vercel/Render"],
            "review": ["테스트 러너", "ESLint", "Playwright", "Security Review"],
        },
        "algorithm": [
            "요구사항을 사용자 흐름, 데이터, API, 화면 단위로 쪼갠다.",
            "기존 코드 구조와 의존성을 조사해 수정 지점을 확정한다.",
            "상태, 오류 처리, 보안, 배포 경로를 포함한 설계를 만든다.",
            "작게 구현하고 각 단위마다 실행 가능한 검증을 붙인다.",
            "런타임 오류, 린트, 회귀, 권한/보안 문제를 검증한다.",
            "실패한 테스트와 사용자 흐름을 기준으로 재수정한다.",
            "배포 후 실제 URL/API 응답까지 확인하면 통과 처리한다.",
        ],
        "acceptance": ["실제 실행됨", "오류/빈 상태 처리됨", "테스트 또는 수동 검증 기록 있음", "배포 URL에서 확인됨"],
    },
    {
        "type": "PPT·발표자료",
        "keywords": ["ppt", "발표", "프레젠테이션", "슬라이드", "강의자료"],
        "tools": {
            "research": ["Perplexity", "NotebookLM"],
            "structure": ["Claude", "Gamma", "Tome"],
            "production": ["Gamma", "Beautiful.ai", "Canva"],
            "review": ["Claude", "Grammarly"],
        },
        "algorithm": [
            "청중, 발표 시간, 설득 목표를 정한다.",
            "핵심 메시지 1개와 보조 근거 3개를 뽑는다.",
            "슬라이드별 제목, 한 줄 메시지, 시각자료 지시를 만든다.",
            "디자인 도구로 초안을 만들고 시각 계층을 정리한다.",
            "텍스트 과밀, 대비, 흐름, 발표 대본 연결성을 검증한다.",
            "문제가 있는 슬라이드를 줄이고 도표/이미지로 대체한다.",
            "발표자 노트와 예상 질문까지 만들면 통과 처리한다.",
        ],
        "acceptance": ["슬라이드당 메시지 1개", "발표 시간 안에 가능", "시각자료가 메시지를 보조", "예상 질문 대응 가능"],
    },
    {
        "type": "이미지·디자인",
        "keywords": ["이미지", "디자인", "로고", "배너", "포스터", "썸네일", "아이콘"],
        "tools": {
            "research": ["Pinterest", "Dribbble", "Perplexity"],
            "structure": ["ChatGPT", "Claude"],
            "production": ["Midjourney", "DALL·E 3", "Canva", "Photoroom"],
            "review": ["Canva", "Claude"],
        },
        "algorithm": [
            "브랜드 톤, 용도, 크기, 금지 요소를 정의한다.",
            "레퍼런스를 모아 색상, 구도, 폰트 방향을 추출한다.",
            "프롬프트와 레이아웃 시안을 여러 버전으로 만든다.",
            "생성/편집 도구로 후보안을 만들고 용도별 크기로 맞춘다.",
            "가독성, 대비, 저작권, 모바일 축소 시 인식성을 검증한다.",
            "선택안의 색/문구/여백을 재조정한다.",
            "원본, 압축본, 투명 배경본을 준비하면 통과 처리한다.",
        ],
        "acceptance": ["작게 봐도 식별됨", "브랜드 톤 일치", "저작권 위험 낮음", "필요 포맷 제공"],
    },
    {
        "type": "데이터 분석",
        "keywords": ["데이터", "엑셀", "분석", "통계", "차트", "csv", "매출", "지표"],
        "tools": {
            "research": ["Julius AI", "ChatExcel"],
            "structure": ["Claude", "ChatGPT"],
            "production": ["Python", "Julius AI", "Excel"],
            "review": ["통계 검증", "데이터 품질 체크"],
        },
        "algorithm": [
            "분석 질문, 기준 기간, 지표 정의를 고정한다.",
            "데이터 타입, 결측치, 이상치, 중복을 점검한다.",
            "집계 기준과 비교군을 설계한다.",
            "표, 차트, 핵심 인사이트를 생성한다.",
            "표본 수, 편향, 단위 오류, 시각화 왜곡을 검증한다.",
            "오류 데이터를 수정하거나 제외 기준을 명시하고 재분석한다.",
            "결론, 한계, 다음 액션이 있으면 통과 처리한다.",
        ],
        "acceptance": ["지표 정의가 명확함", "결측/이상치 처리됨", "차트가 결론을 왜곡하지 않음", "다음 액션 제시"],
    },
    {
        "type": "마케팅·카피",
        "keywords": ["마케팅", "광고", "카피", "랜딩", "세일즈", "브랜딩", "홍보"],
        "tools": {
            "research": ["Perplexity", "Google Trends"],
            "structure": ["Claude", "ChatGPT"],
            "production": ["Copy.ai", "Jasper", "Canva"],
            "review": ["A/B 체크리스트", "Grammarly"],
        },
        "algorithm": [
            "타깃, 문제, 제안, 전환 목표를 정의한다.",
            "경쟁 메시지와 고객 언어를 조사한다.",
            "훅, 가치제안, 증거, CTA 순서로 구조화한다.",
            "채널별 카피와 랜딩 섹션을 만든다.",
            "과장 표현, 신뢰 근거, CTA 일관성을 검증한다.",
            "약한 훅과 모호한 혜택 문구를 재작성한다.",
            "A/B 테스트 후보 3개와 측정 지표를 만들면 통과 처리한다.",
        ],
        "acceptance": ["타깃이 선명함", "혜택이 구체적임", "증거가 있음", "CTA가 하나로 모임"],
    },
    {
        "type": "학습·강의",
        "keywords": ["공부", "학습", "강의", "커리큘럼", "문제", "시험", "교육"],
        "tools": {
            "research": ["NotebookLM", "Perplexity"],
            "structure": ["Claude", "ChatGPT"],
            "production": ["Notion AI", "Quizlet"],
            "review": ["자가진단 퀴즈", "Claude"],
        },
        "algorithm": [
            "학습자 수준, 목표, 기간을 정한다.",
            "필수 개념과 선행 지식을 조사한다.",
            "개념→예제→연습→피드백 순서로 커리큘럼을 만든다.",
            "요약 노트, 예제, 퀴즈를 제작한다.",
            "난이도, 누락 개념, 오답 유도 요소를 검증한다.",
            "틀린 문제 유형을 보강 자료로 재제작한다.",
            "진단 테스트와 복습 계획이 있으면 통과 처리한다.",
        ],
        "acceptance": ["수준에 맞음", "연습 문제가 있음", "오답 피드백 가능", "복습 계획 포함"],
    },
    {
        "type": "리서치·아이디어",
        "keywords": ["조사", "리서치", "아이디어", "시장", "경쟁사", "트렌드"],
        "tools": {
            "research": ["Perplexity", "Google Trends", "Liner"],
            "structure": ["Claude", "Whimsical AI"],
            "production": ["Notion AI", "ChatGPT"],
            "review": ["출처 검증", "반례 체크"],
        },
        "algorithm": [
            "질문 범위와 판단 기준을 정한다.",
            "최신 자료와 1차 출처를 우선 조사한다.",
            "자료를 기회, 위험, 근거, 반례로 분류한다.",
            "실행 가능한 아이디어 후보를 만든다.",
            "출처 신뢰도, 최신성, 편향을 검증한다.",
            "근거가 약한 후보는 폐기하거나 추가 조사한다.",
            "우선순위와 실행 실험안을 제시하면 통과 처리한다.",
        ],
        "acceptance": ["출처가 있음", "반례 검토됨", "우선순위가 있음", "실행 실험 가능"],
    },
    {
        "type": "업무 자동화",
        "keywords": ["자동화", "반복", "봇", "스크립트", "업무", "크롤링", "알림"],
        "tools": {
            "research": ["Docs", "Zapier", "Make"],
            "structure": ["Claude", "ChatGPT"],
            "production": ["Python", "Zapier", "Make", "GitHub Actions"],
            "review": ["로그 점검", "예외 케이스 테스트"],
        },
        "algorithm": [
            "반복 업무의 입력, 처리, 출력, 주기를 정의한다.",
            "권한, API 한도, 실패 시 복구 경로를 조사한다.",
            "트리거, 처리 단계, 저장소, 알림 구조를 설계한다.",
            "작은 자동화부터 구현하고 로그를 남긴다.",
            "중복 실행, 실패 재시도, 보안 키 노출을 검증한다.",
            "예외 케이스를 추가해 재실행한다.",
            "운영 체크리스트와 중단 방법이 있으면 통과 처리한다.",
        ],
        "acceptance": ["반복 실행 가능", "실패 로그 있음", "비밀키 노출 없음", "중단/재시도 가능"],
    },
    {
        "type": "일반·기타",
        "keywords": [],
        "tools": {
            "research": ["Perplexity", "ChatGPT"],
            "structure": ["Claude", "ChatGPT"],
            "production": ["ChatGPT", "Claude", "Canva"],
            "review": ["Claude", "DeepSeek"],
        },
        "algorithm": [
            "작업 목표, 대상, 결과물 형태, 마감을 명확히 한다.",
            "관련 자료·레퍼런스·제약을 조사한다.",
            "단계별 실행 계획과 산출물 목록을 만든다.",
            "초안·시안·프로토타입을 제작한다.",
            "요구사항 충족, 품질, 일관성을 검증한다.",
            "피드백을 반영해 수정한다.",
            "최종 결과물과 다음 액션을 정리하면 통과 처리한다.",
        ],
        "acceptance": ["목표가 달성됨", "형식·분량 준수", "품질 기준 충족", "다음 단계가 명확함"],
    },
]


COLLAB_STAGE_META = [
    {
        "short": "조사",
        "role": "조사·리서치",
        "default_model": "Perplexity",
        "options": ["Perplexity", "xAI", "OpenAI", "Google"],
        "hint": "요청의 핵심 목표·제약·독자를 파악하고, 최신 출처·팩트·레퍼런스·경쟁/유사 사례를 수집합니다. 다음 단계(구조)가 바로 쓸 수 있는 근거 목록을 만듭니다.",
        "detail": "① 작업 범위·성공 기준 정의 ② 1차 출처·최신 자료 수집 ③ 핵심 인사이트·리스크·반례 정리 ④ 구조 단계용 근거 패키지 작성",
    },
    {
        "short": "구조",
        "role": "구조·기획",
        "default_model": "Anthropic",
        "options": ["Anthropic", "OpenAI", "Google", "DeepSeek"],
        "hint": "조사 결과를 바탕으로 목차·논리 흐름·섹션별 핵심 메시지·작성 지시문(프롬프트)을 설계합니다. 제작 AI가 그대로 실행할 수 있는 청사진을 만듭니다.",
        "detail": "① 목차/와이어프레임 ② 섹션별 목적·필수 포함 요소 ③ 논리 순서·전환 ④ 제작 단계용 상세 지시문",
    },
    {
        "short": "제작",
        "role": "제작·초안",
        "default_model": "OpenAI",
        "options": ["OpenAI", "Anthropic", "DeepSeek", "Google"],
        "hint": "구조·지시문에 따라 실제 작업물 초안을 작성합니다. 조사·구조 단계 산출물을 반영하고, 검증 단계에서 점검할 완성형 초안을 만듭니다.",
        "detail": "① 구조대로 본문/초안 작성 ② 형식·분량·톤 준수 ③ 근거·출처 반영 ④ 검증용 최종 초안 제출",
    },
    {
        "short": "검증",
        "role": "검증·품질",
        "default_model": "DeepSeek",
        "options": ["DeepSeek", "Anthropic", "OpenAI", "Perplexity"],
        "hint": "작업물을 직접 재작성하지 않습니다. 오류·누락·논리 비약·형식 문제를 지적하고, 조사/구조/제작 AI에게 줄 구체적 수정 지시를 작성합니다.",
        "detail": "① 통과 기준 대비 체크 ② 오류·보완점 목록(심각도) ③ 단계별 수정 지시 ④ 통과/재작업 필요 여부(JSON)",
    },
]

COLLAB_TYPE_DEFAULT_MODELS: dict[str, list[str]] = {
    "자기소개서·취업": ["Perplexity", "Anthropic", "OpenAI", "DeepSeek"],
    "문서 제작": ["Perplexity", "Anthropic", "OpenAI", "DeepSeek"],
    "동영상 제작": ["Perplexity", "xAI", "OpenAI", "DeepSeek"],
    "PPT·발표자료": ["Perplexity", "Anthropic", "OpenAI", "DeepSeek"],
    "앱·웹 개발": ["Perplexity", "Anthropic", "OpenAI", "DeepSeek"],
    "이미지·디자인": ["Google", "Anthropic", "OpenAI", "DeepSeek"],
    "리서치·아이디어": ["Perplexity", "xAI", "Anthropic", "DeepSeek"],
    "일반·기타": ["Perplexity", "Anthropic", "OpenAI", "DeepSeek"],
}


def _score_collab_rule(task_lower: str, rule: dict) -> int:
    score = 0
    for keyword in rule["keywords"]:
        kw = keyword.lower()
        if kw in task_lower:
            score += max(3, len(kw))
    return score


def _select_collab_rule(task: str, forced_type: str | None = None) -> dict:
    if forced_type:
        for rule in COLLAB_TYPE_RULES:
            if rule["type"] == forced_type:
                return rule
    lowered = task.lower()
    general = COLLAB_TYPE_RULES[-1]
    selected = general
    best_score = 0
    for rule in COLLAB_TYPE_RULES[:-1]:
        score = _score_collab_rule(lowered, rule)
        if score > best_score:
            best_score = score
            selected = rule
    return selected if best_score > 0 else general


COLLAB_INTAKE_GUIDES: dict[str, list[str]] = {
    "자기소개서·취업": [
        "지원하시는 **회사·직무·채용 공고(JD)** 링크나 핵심 요구사항을 알려주세요.",
        "**분량**(글자 수·항목 수)과 **제출 마감**이 있나요?",
        "강조하고 싶은 **경력·성과·프로젝트**를 구체적 수치와 함께 적어주세요.",
        "**회사/직무에 맞춰 꼭 넣을 키워드**나 차별화 포인트가 있나요?",
        "피해야 할 표현, 참고할 **톤·샘플**, 면접관이 보는 포인트가 있다면 알려주세요.",
        "**학력·자격·어학·수상** 중 반드시 넣을 항목이 있나요?",
        "경력 기술 시 **STAR(상황·과제·행동·결과)** 로 쓸 에피소드 2~3개를 적어주세요.",
        "다른 지원서·포트폴리오와 **중복되면 안 되는** 경험이 있나요?",
    ],
    "동영상 제작": [
        "**플랫폼**(유튜브·릴스·틱톡 등)과 **목표 길이**는 어떻게 되나요?",
        "**타깃 시청자**와 영상의 **핵심 메시지·CTA**를 알려주세요.",
        "원하는 **톤·스타일·레퍼런스 영상** 링크가 있나요?",
        "**촬영/편집 가능 범위**(본인 촬영, AI 생성, 스톡 등)와 **마감**은?",
        "필수로 들어갈 **대사·브랜드 요소·금지 사항**이 있나요?",
        "**썸네일·제목·설명란**까지 함께 만들까요? 키워드가 있다면 알려주세요.",
        "저작권·음원·얼굴 초상권 등 **법적 제약**이 있나요?",
    ],
    "문서 제작": [
        "문서 **목적·독자·용도**(제출처)와 **분량·형식**을 알려주세요.",
        "**필수 포함 항목·목차** 요구가 있나요?",
        "참고할 **자료·링크·기존 초안**이 있나요? (없으면 「없음」)",
        "**마감**과 **톤**(격식/캐주얼) 기준은?",
        "특히 강조할 **주장·데이터·사례**가 있다면 적어주세요.",
    ],
    "앱·웹 개발": [
        "만들려는 **기능·화면·사용자 흐름**을 한 줄로 요약해 주세요.",
        "**기술 스택·언어·프레임워크** 선호나 제약이 있나요?",
        "**배포 환경**(웹/앱, URL, 서버)과 **우선순위 기능**은?",
        "기존 **코드·레포·API**가 있다면 공유 가능한 범위를 알려주세요.",
        "**마감**과 **완료 기준**(어떤 상태면 끝인지)을 정해 주세요.",
        "**디자인/UI** 요구(와이어프레임, 참고 앱)가 있나요?",
        "**로그인·결제·푸시** 등 필수/제외 기능을 구분해 주세요.",
    ],
    "PPT·발표자료": [
        "**청중·발표 장소·발표 시간**을 알려주세요.",
        "**슬라이드 수·템플릿·브랜드 가이드** 요구가 있나요?",
        "전달할 **핵심 메시지 1~2개**와 **반드시 넣을 내용**은?",
        "참고 **자료·데이터·차트**가 있나요?",
        "**마감**과 발표 후 **Q&A·예상 질문** 준비가 필요한가요?",
    ],
    "이미지·디자인": [
        "**용도·크기·포맷**(SNS, 인쇄, 앱 아이콘 등)을 알려주세요.",
        "**브랜드 색·폰트·금지 요소**가 있나요?",
        "원하는 **분위기·레퍼런스 이미지** 링크가 있나요?",
        "들어갈 **텍스트·로고·필수 요소**를 적어주세요.",
        "**마감**과 **최종 파일 형식**(PNG, SVG 등)은?",
    ],
    "데이터 분석": [
        "**분석 질문·목표**와 **데이터 파일/출처**를 알려주세요.",
        "**기간·비교 기준·핵심 KPI** 정의가 있나요?",
        "원하는 **산출물**(표, 차트, 인사이트 문장) 형태는?",
        "**도구 선호**(Excel, Python 등)나 **제약**이 있나요?",
        "**마감**과 의사결정에 쓸 **핵심 결론** 방향은?",
    ],
    "마케팅·카피": [
        "**타깃 고객·제품·전환 목표**(구매, 가입 등)를 알려주세요.",
        "**채널**(랜딩, SNS, 광고)과 **분량·톤**은?",
        "경쟁사 대비 **차별점·증거·수치**가 있나요?",
        "**금지 표현·브랜드 가이드**가 있나요?",
        "**마감**과 A/B 테스트 등 **성공 지표**는?",
    ],
    "학습·강의": [
        "**학습자 수준·목표·기간**을 알려주세요.",
        "다룰 **주제 범위·교재·선행 지식**은?",
        "원하는 **형식**(요약, 문제, 강의안)과 **분량**은?",
        "**시험·평가** 유형이 있다면 알려주세요.",
        "**마감**과 반드시 포함할 **개념·예제**가 있나요?",
    ],
    "리서치·아이디어": [
        "**조사 질문·판단 기준·범위**를 알려주세요.",
        "**산업·시장·지역** 등 조사 범위는?",
        "원하는 **산출물**(요약, 비교표, 아이디어 목록) 형태는?",
        "**최신성·출처** 요구 수준이 있나요?",
        "**마감**과 의사결정에 필요한 **핵심 결론** 방향은?",
    ],
    "업무 자동화": [
        "자동화할 **반복 업무**의 입력→처리→출력 흐름을 설명해 주세요.",
        "**실행 주기·트리거**(시간, 이메일, 파일 등)는?",
        "사용 중인 **도구·API·권한** 제약이 있나요?",
        "**실패 시 알림·복구** 요구가 있나요?",
        "**마감**과 **완료 기준**(어떤 상태면 성공인지)은?",
    ],
    "_default": [
        "이 작업의 **최종 목표·결과물 형태·사용처**를 구체적으로 알려주세요.",
        "**분량·형식·마감** 기준이 있나요?",
        "꼭 **포함·강조할 내용**과 **피해야 할 것**을 적어주세요.",
        "참고 **자료·링크·샘플**이 있나요? (없으면 「없음」)",
        "대상 **독자/사용자**가 특히 중요하게 보는 포인트가 있나요?",
        "추가로 반영할 내용이 더 있나요? (없으면 「없음」)",
    ],
}


def _collab_intake_questions_for_task(task: str) -> tuple[str, list[str]]:
    rule = _select_collab_rule(task)
    work_type = rule["type"]
    questions = COLLAB_INTAKE_GUIDES.get(work_type) or COLLAB_INTAKE_GUIDES["_default"]
    return work_type, questions


def build_collab_plan(task: str, forced_type: str | None = None) -> dict:
    selected = _select_collab_rule(task, forced_type)
    tools = selected["tools"]
    stages = [
        {
            "name": "1단계 · 조사/아이디어",
            "goal": COLLAB_STAGE_META[0]["hint"],
            "detail": COLLAB_STAGE_META[0]["detail"],
            "tools": tools["research"],
            "actions": selected["algorithm"][:2],
        },
        {
            "name": "2단계 · 논리구조/지시문",
            "goal": COLLAB_STAGE_META[1]["hint"],
            "detail": COLLAB_STAGE_META[1]["detail"],
            "tools": tools["structure"],
            "actions": selected["algorithm"][2:3],
        },
        {
            "name": "3단계 · 제작/초안",
            "goal": COLLAB_STAGE_META[2]["hint"],
            "detail": COLLAB_STAGE_META[2]["detail"],
            "tools": tools["production"],
            "actions": selected["algorithm"][3:4],
        },
        {
            "name": "4단계 · 검증/피드백",
            "goal": COLLAB_STAGE_META[3]["hint"],
            "detail": COLLAB_STAGE_META[3]["detail"],
            "tools": tools["review"],
            "actions": selected["algorithm"][4:6],
        },
    ]
    stage_models = COLLAB_TYPE_DEFAULT_MODELS.get(
        selected["type"],
        [m["default_model"] for m in COLLAB_STAGE_META],
    )
    stage_recommendations = []
    for i, meta in enumerate(COLLAB_STAGE_META):
        model = stage_models[i] if i < len(stage_models) else meta["default_model"]
        alts = [m for m in meta["options"] if m != model][:2]
        stage_recommendations.append(
            {
                "index": i + 1,
                "model": model,
                "reason": meta["hint"],
                "alternatives": alts,
            }
        )
    return {
        "task": task,
        "work_type": selected["type"],
        "summary": (
            f"「{selected['type']}」 작업으로 분류했습니다. "
            "1) 조사 → 2) 구조·지시문 → 3) 제작 → 4) 검증(피드백·지시) 순으로 단계별 실행합니다. "
            "검증 AI는 재작성하지 않고, 문제 지적 후 해당 단계 AI가 수정합니다."
        ),
        "stage_models": stage_models,
        "stage_meta": COLLAB_STAGE_META,
        "stage_recommendations": stage_recommendations,
        "stages": stages,
        "acceptance": selected["acceptance"],
        "handoff": "각 단계 산출물을 다음 단계 입력으로 넘깁니다. 검증 실패 시 지적·지시만 전달하고 조사/구조/제작 단계가 순서대로 수정합니다.",
    }


class AgentRequest(BaseModel):
    query: str
    user_id: str = ""


class AgentAskRequest(BaseModel):
    query: str
    history: list[dict] = Field(default_factory=list)
    user_id: str = ""


# /agent/step (확장 기반 무상태 두뇌)용 요청 모델.
# [보안] elements에는 비밀번호/카드번호 등 민감 입력값이 들어오면 안 된다.
# 확장(background.js)의 스캔이 input[type="password"] 값은 수집하지 않고
# has_password 플래그만 보낸다. 서버는 만약을 대비해 custom_agent.detect_handoff
# 에서 비밀번호/캡차/결제 폼을 만나면 LLM 호출 없이 즉시 사용자 핸드오프로 정지한다.
class AgentStepRequest(BaseModel):
    task: str
    mission_id: str = ""
    elements: list[dict] = Field(default_factory=list)
    current_url: str = ""
    action_history: list[dict] = Field(default_factory=list)
    user_id: str = ""


class DebateRequest(BaseModel):
    session_id: str
    topic: str
    user_input: str | None = None
    retry_speaker_index: int | None = None
    user_id: str = ""


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
DEBATE_AI_SEMAPHORE = asyncio.Semaphore(2)
COMPARE_ACTIVE_MODELS: dict[str, set[str]] = {}
COMPARE_SESSION_PLANS: dict[str, dict[str, str]] = {}
COMPARE_SESSION_PENDING: dict[str, int] = {}
COMPARE_SESSION_LOCK = asyncio.Lock()
COMPARE_QUOTA_CHARGED: set[str] = set()
COMPARE_QUOTA_LOCK = asyncio.Lock()
AGENT_LOCK = asyncio.Semaphore(1)

# /agent/step은 브라우저를 띄우지 않는 순수 LLM 판단이라 부담이 훨씬 적다.
# /agent/task의 AGENT_LOCK(1개)과 공유하지 않고 별도 세마포어로 넉넉히 허용한다.
AGENT_STEP_CONCURRENCY = int(os.environ.get("AGENT_STEP_CONCURRENCY", "8"))
AGENT_STEP_SEMAPHORE = asyncio.Semaphore(AGENT_STEP_CONCURRENCY)


def get_openrouter_api_key() -> str:
    """Return OpenRouter API key from env. Only OpenRouter-format keys are accepted."""
    for name in ("OPENROUTER_API_KEY", "OPENROUTER_KEY"):
        val = os.environ.get(name, "").strip()
        if val:
            return val
    # Legacy: OPENAI_API_KEY only if it is actually an OpenRouter key (sk-or-v1-…)
    openai_env = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai_env and classify_api_key(openai_env) == "openrouter":
        return openai_env
    return ""


def get_openrouter_key_source() -> str:
    for name in ("OPENROUTER_API_KEY", "OPENROUTER_KEY"):
        if os.environ.get(name, "").strip():
            return name
    openai_env = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai_env and classify_api_key(openai_env) == "openrouter":
        return "OPENAI_API_KEY (openrouter-format)"
    return ""


def require_openrouter_key() -> str:
    key = get_openrouter_api_key()
    if not key:
        raise HTTPException(
            status_code=503,
            detail="OPENROUTER_API_KEY가 설정되지 않았습니다. Render에 OpenRouter 키(sk-or-v1-…)를 등록하세요.",
        )
    return key


def classify_api_key(key: str) -> str:
    k = key.strip()
    if not k:
        return "missing"
    low = k.lower()
    if low.startswith("sk-or-"):
        return "openrouter"
    if low.startswith("sk-proj-") or low.startswith("sk-"):
        return "openai"
    return "unknown"


OPENROUTER_AUTH_ERROR_MSG = (
    "OpenRouter 인증 실패입니다. Render에 OPENROUTER_API_KEY(openrouter.ai/keys)를 설정하세요. "
    "OPENAI_API_KEY만 있고 OpenAI 전용 키(sk-…)라면 Nasaro AI가 동작하지 않습니다."
)


async def verify_openrouter_auth() -> tuple[bool | None, str | None]:
    """Probe OpenRouter with the configured key (cached callers should not spam)."""
    api_key = get_openrouter_api_key()
    if not api_key:
        return None, None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=8.0)) as client:
            response = await client.post(
                OPENROUTER_CHAT_URL,
                headers=build_openrouter_headers(),
                json={
                    "model": LAST_RESORT_MODEL,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
    except httpx.HTTPError as exc:
        return False, f"OpenRouter 연결 실패: {exc.__class__.__name__}"

    if response.status_code == 200:
        return True, None
    if response.status_code == 401:
        if classify_api_key(api_key) == "openai":
            return False, OPENROUTER_AUTH_ERROR_MSG
        return False, "OpenRouter 인증 실패(401). API 키를 확인하세요."
    snippet = response.text[:160].replace("\n", " ")
    return False, f"OpenRouter 오류({response.status_code}): {snippet}"


def build_openrouter_headers() -> dict[str, str]:
    api_key = require_openrouter_key()
    return {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": os.environ.get("OPENROUTER_HTTP_REFERER", "https://nasaroai.onrender.com"),
        "X-Title": os.environ.get("OPENROUTER_APP_TITLE", "Nasaro AI"),
        "Content-Type": "application/json",
    }


def log_openrouter_key_status() -> None:
    api_key = get_openrouter_api_key()
    prefix = api_key[:12] if api_key else "N/A"
    if not api_key:
        openai_only = os.environ.get("OPENAI_API_KEY", "").strip()
        if openai_only and classify_api_key(openai_only) != "openrouter":
            logger.warning(
                "OPENROUTER_API_KEY missing; OPENAI_API_KEY is OpenAI-only (sk-…) — "
                "Nasaro AI requires an OpenRouter key in OPENROUTER_API_KEY"
            )
        elif openai_only:
            logger.warning("OPENROUTER_API_KEY empty; using OpenRouter-format key from OPENAI_API_KEY")
        else:
            logger.warning("OPENROUTER_API_KEY is empty")
        return
    source = get_openrouter_key_source() or "OPENROUTER_API_KEY"
    logger.info(
        "OpenRouter API ready via %s. length=%d prefix=%s type=%s",
        source,
        len(api_key),
        prefix,
        classify_api_key(api_key),
    )


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
    if model_id in MODEL_CACHE_STATE.all_model_ids:
        return model_id
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
    """Ordered preference chain: paid primary, free substitutes, then catalog extras."""
    config = LABEL_PROVIDER_CONFIG[label]
    chain: list[str] = []

    if config.official_model_id:
        chain.append(config.official_model_id)

    for substitute in config.substitute_chain:
        resolved = resolve_catalog_model(substitute) or substitute
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
    """Ordered unique candidates: label chain first, then global free fallback pool."""
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
        headers: dict[str, str] = {}
        try:
            headers = build_openrouter_headers()
        except HTTPException:
            headers = {}
        async with httpx.AsyncClient(timeout=MODEL_REFRESH_TIMEOUT) as client:
            response = await client.get(OPENROUTER_MODELS_URL, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        logger.exception("Failed to fetch OpenRouter model catalog. Using last-resort fallback.")
        configured_ids = [
            config.official_model_id
            for config in LABEL_PROVIDER_CONFIG.values()
            if config.official_model_id
        ]
        MODEL_CACHE_STATE.all_model_ids = set(configured_ids + [LAST_RESORT_MODEL])
        MODEL_CACHE_STATE.free_model_ids = {LAST_RESORT_MODEL}
        MODEL_CACHE_STATE.all_free_models = [LAST_RESORT_MODEL]
        MODEL_CACHE_STATE.free_models_by_label = {"OpenAI": [LAST_RESORT_MODEL]}
        MODEL_CACHE_STATE.error = str(exc)
        rebuild_model_mappings("last_resort")
        return

    free_models_by_label: dict[str, list[str]] = {label: [] for label in COMPANY_LABELS}
    all_free_models: list[str] = []
    all_model_ids: list[str] = []

    for model in payload.get("data", []):
        model_id = model.get("id")
        if not isinstance(model_id, str):
            continue

        all_model_ids.append(model_id)
        if not is_free_model(model):
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

    MODEL_CACHE_STATE.all_model_ids = set(all_model_ids)
    MODEL_CACHE_STATE.free_model_ids = set(all_free_models)
    MODEL_CACHE_STATE.all_free_models = unique(all_free_models)
    MODEL_CACHE_STATE.free_models_by_label = free_models_by_label
    MODEL_CACHE_STATE.error = None
    rebuild_model_mappings("openrouter_catalog")
    logger.info("Loaded OpenRouter model cache: %s", MODEL_CANDIDATES)


async def ensure_model_cache_fresh() -> None:
    is_empty = not MODEL_CACHE_STATE.loaded
    is_stale = time.time() - MODEL_CACHE_STATE.refreshed_at > MODEL_CACHE_TTL_SECONDS
    if not is_empty and not is_stale:
        return

    async with MODEL_CACHE_LOCK:
        is_empty = not MODEL_CACHE_STATE.loaded
        is_stale = time.time() - MODEL_CACHE_STATE.refreshed_at > MODEL_CACHE_TTL_SECONDS
        if is_empty or is_stale:
            await refresh_model_cache()


@app.on_event("startup")
async def startup() -> None:
    log_openrouter_key_status()
    await ensure_model_cache_fresh()


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
    return status_code in {400, 401, 402, 403, 404, 408, 409, 429, 500, 502, 503, 504}


def is_daily_free_limit(body_text: str, model_id: str = "") -> bool:
    """Detect OpenRouter's account-wide per-day free quota exhaustion."""
    lowered = (body_text or "").lower()
    if "free-models-per-day" in lowered:
        return True
    if ":free" in model_id and "add 10 credits" in lowered:
        return True
    return False


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


async def _call_ai_model_core(
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
                if is_daily_free_limit(response.text, model_id):
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
                if response.status_code == 401:
                    return make_failed_result(
                        requested_label=label,
                        requested_model=requested_model,
                        actual_model=model_id,
                        failed_candidates=failed_candidates,
                        error=OPENROUTER_AUTH_ERROR_MSG,
                    )
                if is_daily_free_limit(response.text, model_id):
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


async def call_ai_model(
    label: str,
    prompt: str,
    max_tokens: int | None = None,
    excluded_models: set[str] | None = None,
) -> ModelCallResult:
    async with DEBATE_AI_SEMAPHORE:
        return await _call_ai_model_core(label, prompt, max_tokens, excluded_models)


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
            f"{SPEAKER1_DIRECTIVE}\n\n"
            f"주제(질문): {topic}\n\n"
            "아직 다른 발언이 없습니다. 이 질문에 대한 자신의 답(주장+근거)만 작성하세요."
        )

    if speaker_index == 1:
        if user_input:
            return (
                f"{SPEAKER1_DIRECTIVE}\n\n"
                f"주제(질문): {topic}\n\n"
                f"지금까지 논의 요약: {previous_summary or '없음'}\n\n"
                f"사용자가 추가 질문/의견을 남겼습니다: '{user_input}'\n\n"
                "이 내용을 반영해 주제에 대한 자신의 답(주장+근거)만 작성하세요. "
                "다른 발언자 평가·전체 요약 금지."
            )
        return (
            f"{SPEAKER1_DIRECTIVE}\n\n"
            f"주제(질문): {topic}\n\n"
            f"이전 라운드 요약:\n{previous_summary or '없음'}\n\n"
            "새 라운드 1번으로, 주제에 대한 새 관점의 답(주장+근거)만 작성하세요."
        )

    role_instruction = "앞선 답을 읽고, 주제에 대해 타당한 점은 인정하고 부족한 점은 비판·보완한 뒤 자신의 답(주장+근거)을 제시"
    compress_older = False
    if speaker_index == 3:
        role_instruction = (
            "1·2번의 답을 검토해 각각 수용·비판할 점을 짚고, "
            "주제(질문)에 대한 자신의 최종 답(입장+근거)을 제시. "
            "형식적 '종합'만 하지 말고 질문에 대한 실질적 답을 포함"
        )
        compress_older = False
    elif speaker_index == 2:
        role_instruction = (
            "1번의 답을 읽고, 주제에 대해 맞는 점은 인정하고, "
            "틀리거나 빠진 점은 근거와 함께 비판·보완한 뒤 "
            "자신이 이 주제에 어떻게 답하는지(주장+근거) 제시 (무조건 반대만 금지)"
        )
        compress_older = False
    else:
        compress_older = len(prior_turns) >= 2
    prior_text = format_prior_turns(prior_turns, compress_older=compress_older)
    return (
        f"{DEBATE_DIRECTIVE}\n\n"
        f"주제(질문): {topic}\n\n"
        f"같은 주제에 대한 앞선 발언:\n\n"
        f"{prior_text}\n\n"
        f"당신은 {speaker_index}번 발언자입니다. {role_instruction}하세요."
    )


def build_summary_prompt(existing_summary: str, turns: list[DebateTurn]) -> str:
    previous = existing_summary or "이전 누적 요약은 없습니다."
    return (
        f"기존 누적 요약:\n{previous}\n\n"
        "직전 라운드의 전체 원문입니다.\n\n"
        f"{format_prior_turns(turns)}\n\n"
        "위 내용을 의미 있게 압축해 누적 요약을 갱신하세요. "
        "'1번(라벨)은 ~라고 주장했고, 2번(라벨)은 ~를 수용·비판·보완했고, 3번(라벨)은 ~라고 종합했다' "
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


def _ensure_debate_sessions_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS debate_sessions (
            session_id TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )


def store_debate_session(session_id: str, session: DebateSession) -> None:
    DEBATE_SESSIONS[session_id] = session
    try:
        with db_connection() as conn:
            _ensure_debate_sessions_table(conn)
            conn.execute(
                "INSERT OR REPLACE INTO debate_sessions (session_id, data, updated_at) VALUES (?, ?, ?)",
                (session_id, session.model_dump_json(), time.time()),
            )
    except Exception:
        logger.exception("Failed to persist debate session session_id=%s", session_id[:8])


def load_debate_session(session_id: str) -> DebateSession | None:
    cached = DEBATE_SESSIONS.get(session_id)
    if cached is not None:
        return cached
    try:
        with db_connection() as conn:
            _ensure_debate_sessions_table(conn)
            row = conn.execute(
                "SELECT data FROM debate_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        session = DebateSession.model_validate_json(row[0])
        DEBATE_SESSIONS[session_id] = session
        return session
    except Exception:
        logger.exception("Failed to load debate session session_id=%s", session_id[:8])
        return None


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
    2: "2번 비평·보완",
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
    speaker_max_tokens = 380 if speaker_index == 1 else None
    for _ in range(MAX_MODEL_CANDIDATES_PER_LABEL):
        result = await call_ai_model(
            requested_label,
            prompt,
            max_tokens=speaker_max_tokens,
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
    sid = session_id[:8] if session_id else ""
    logger.info("ensure_compare_session_plan start session_id=%s", sid)
    if not session_id:
        logger.info("ensure_compare_session_plan done session_id=%s plan_size=0", sid)
        return {}

    async with COMPARE_SESSION_LOCK:
        existing = COMPARE_SESSION_PLANS.get(session_id)
        if existing is not None:
            logger.info("ensure_compare_session_plan done session_id=%s plan_size=%d", sid, len(existing))
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
        logger.info("ensure_compare_session_plan done session_id=%s plan_size=%d", sid, len(plan))
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
    sid = session_id[:8] if session_id else ""
    logger.info("acquire_compare_model start session_id=%s label=%s", sid, label)
    if not session_id:
        pool = build_model_try_order(label, excluded_by_failure)
        model_id = pool[0] if pool else None
        logger.info("acquire_compare_model done session_id=%s label=%s model=%s", sid, label, model_id)
        return model_id

    plan = await ensure_compare_session_plan(session_id)
    pool = build_compare_candidate_pool(label, plan, excluded_by_failure)
    if not pool:
        logger.info("acquire_compare_model done session_id=%s label=%s model=None", sid, label)
        return None

    async with COMPARE_SESSION_LOCK:
        used = COMPARE_ACTIVE_MODELS.setdefault(session_id, set())
        for model_id in pool:
            if model_id not in used:
                used.add(model_id)
                logger.info("acquire_compare_model done session_id=%s label=%s model=%s", sid, label, model_id)
                return model_id
        logger.warning(
            "Compare model pool exhausted session_id=%s label=%s used=%s pool=%s",
            session_id[:8],
            label,
            sorted(used),
            pool,
        )
        logger.info("acquire_compare_model done session_id=%s label=%s model=None", sid, label)
        return None


async def mark_compare_stream_started(session_id: str) -> None:
    if not session_id:
        return
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
async def stream_compare(data: CompareRequest, request: Request) -> StreamingResponse:
    logger.info(
        "compare/stream request model=%s session=%s client=%s",
        data.model_name,
        data.compare_session_id[:8] if data.compare_session_id else "",
        request.client.host if request.client else "?",
    )
    if data.model_name not in COMPANY_LABELS:
        raise HTTPException(status_code=400, detail="Invalid model_name")

    label = data.model_name
    persona = PERSONAS[label]
    session_id = data.compare_session_id.strip()

    async def generate() -> AsyncIterator[str]:
        yield ": keepalive\n\n"
        current_model_id: str | None = None
        stream_started = False
        try:
            try:
                await _require_compare_coin(request, data.user_id)
            except HTTPException as exc:
                yield sse(
                    {
                        "model": data.model_name,
                        "success": False,
                        "error": _quota_error_message(exc.detail),
                    }
                )
                return

            if not MODEL_CACHE_STATE.loaded:
                await ensure_model_cache_fresh()
            await ensure_compare_session_plan(session_id)

            excluded_by_failure: set[str] = set()
            await mark_compare_stream_started(session_id)
            stream_started = True

            while len(excluded_by_failure) < MAX_COMPARE_STREAM_MODEL_ATTEMPTS:
                yield ": keepalive\n\n"
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
                    async with httpx.AsyncClient(timeout=COMPARE_STREAM_TIMEOUT) as client:
                        for attempt in range(2):
                            yield ": keepalive\n\n"
                            logger.info(
                                "compare/stream OpenRouter request start label=%s model=%s session=%s attempt=%d",
                                label,
                                model_id,
                                session_id[:8] if session_id else "",
                                attempt + 1,
                            )
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
                                logger.info(
                                    "compare/stream OpenRouter response label=%s model=%s status=%s",
                                    label,
                                    model_id,
                                    response.status_code,
                                )
                                if response.status_code == 429:
                                    body = (await response.aread()).decode("utf-8", "ignore")
                                    if is_daily_free_limit(body, model_id):
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
                                        "Stream non-200 label=%s model=%s status=%s body=%s",
                                        label,
                                        model_id,
                                        response.status_code,
                                        body[:300],
                                    )
                                    if response.status_code == 401:
                                        yield sse({"model": data.model_name, "success": False, "error": OPENROUTER_AUTH_ERROR_MSG})
                                        return
                                    if is_daily_free_limit(body, model_id):
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
                except HTTPException as exc:
                    detail = exc.detail
                    if exc.status_code == 503 and isinstance(detail, str) and "OPENROUTER" in detail.upper():
                        error_msg = OPENROUTER_AUTH_ERROR_MSG
                    elif exc.status_code == 429:
                        error_msg = _quota_error_message(detail)
                    elif isinstance(detail, str):
                        error_msg = detail
                    else:
                        error_msg = COMPARE_FAILURE_MSG
                    yield sse({"model": data.model_name, "success": False, "error": error_msg})
                    return

                if not failed_this_model:
                    return

            yield sse({"model": data.model_name, "success": False, "error": COMPARE_FAILURE_MSG})
        except Exception:
            logger.exception("compare/stream generate failed label=%s session=%s", label, session_id[:8])
            yield sse({"model": data.model_name, "success": False, "error": COMPARE_FAILURE_MSG})
        finally:
            if current_model_id:
                await release_compare_model(session_id, current_model_id)
            if stream_started:
                await mark_compare_stream_done(session_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


COLLAB_INTAKE_MAX_COINS = 10


@app.post("/collab/intake")
async def collab_intake(data: CollabIntakeRequest, request: Request) -> dict:
    """AI 추천 전 작업 세부사항을 채팅으로 수집한다 (1 coin/질문, 최대 10 coin)."""
    await ensure_model_cache_fresh()
    task = data.task.strip()
    if not task:
        raise HTTPException(status_code=400, detail="작업 내용을 입력하세요.")

    intake_model = (data.intake_model or "Claude").strip() or "Claude"
    user_turns = sum(1 for m in data.messages if (m.role or "").strip() == "user")
    asked_questions = [
        (m.content or "").strip()
        for m in data.messages
        if (m.role or "").strip().lower() == "assistant"
        and (m.content or "").strip()
        and not (m.content or "").strip().startswith("질문을 준비")
    ]
    user_facts = [
        (m.content or "").strip()
        for m in data.messages
        if (m.role or "").strip().lower() == "user" and (m.content or "").strip()
    ]
    history_lines: list[str] = []
    for m in data.messages:
        role = (m.role or "").strip().lower()
        prefix = "사용자" if role == "user" else "기획 AI"
        content = (m.content or "").strip()
        if content:
            history_lines.append(f"{prefix}: {content}")
    history_text = "\n".join(history_lines) if history_lines else f"사용자: {task}"

    asked_block = "\n".join(f"- {q}" for q in asked_questions[-12:]) or "(없음)"
    facts_block = "\n".join(f"- {u}" for u in user_facts[-12:]) or "(없음)"

    work_type, type_questions = _collab_intake_questions_for_task(task)
    type_q_block = "\n".join(f"- {q}" for q in type_questions[:8])
    selected_rule = _select_collab_rule(task)
    acceptance_hint = ", ".join(selected_rule.get("acceptance", [])[:4])

    prompt = (
        f"당신은 **{intake_model}** AI 협업 기획 파트너입니다. "
        f"사용자와 대화하며 「{work_type}」 작업을 실제로 끝내기 위해 필요한 정보를 수집합니다. "
        f"형식적인 인사·빈 질문·같은 질문 반복은 금지입니다.\n\n"
        f"[추정 작업 유형] {work_type}\n"
        f"[완료 기준 참고] {acceptance_hint}\n\n"
        f"[초기 요청]\n{task}\n\n"
        f"[지금까지 대화]\n{history_text}\n\n"
        f"[이미 물어본 질문 — 절대 반복·유사 질문 금지]\n{asked_block}\n\n"
        f"[사용자가 이미 말한 사실]\n{facts_block}\n\n"
        f"[{work_type}에서 아직 다루지 않은 정보 영역 — 참고용, 그대로 복붙 금지]\n"
        f"{type_q_block}\n\n"
        "지침:\n"
        f"- {intake_model} AI처럼 말하세요. 사용자 요청을 1문장으로 짧게 인지한 뒤 "
        "**한 번에 하나의 구체적·실무적 질문**만 하세요.\n"
        f"- **{work_type}**에 실제로 필요한 정보(목적·대상·분량·마감·제약·레퍼런스·톤·기술스택 등)를 "
        "**서로 다른 각도**에서 순서대로 수집하세요.\n"
        "- 이미 답한 내용·위 질문과 **의미가 겹치면 절대 금지**. 다음으로 아직 없는 핵심만 물으세요.\n"
        "- 「더 알려주세요」「어떤 작업인가요」「목표가 무엇인가요」 같은 **빈 질문 금지**.\n"
        "- 질문은 실행 가능하게 (예: '자기소개서 분량이 몇 자인가요?' / '타깃 연령대는?' / '사용할 프레임워크는?').\n"
        "- 핵심 정보가 충분하면 ready=true (보통 4~8회 질문).\n"
        "- ready=true일 때 enriched_task에 수집한 모든 정보를 통합한 완전한 작업 지시문을 작성하세요.\n\n"
        'JSON만 출력: {"ready": false, "question": "..."} 또는 '
        '{"ready": true, "enriched_task": "통합 작업 설명..."}'
    )

    def _norm_q(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").lower().replace("?", "").replace("！", "").strip())

    def _is_repeat_question(question: str, prev: list[str]) -> bool:
        qn = _norm_q(question)
        if not qn or len(qn) < 6:
            return False
        for old in prev:
            on = _norm_q(old)
            if not on:
                continue
            if qn == on or qn in on or on in qn:
                return True
            q_words = set(qn.split())
            o_words = set(on.split())
            if len(q_words & o_words) >= max(3, min(len(q_words), len(o_words)) // 2):
                return True
        return False

    fallback_questions = type_questions + COLLAB_INTAKE_GUIDES["_default"]

    def _next_fallback_question() -> str | None:
        for q in fallback_questions:
            if not _is_repeat_question(q, asked_questions):
                return q
        return None

    ai_calls_done = len(asked_questions)
    if ai_calls_done >= COLLAB_INTAKE_MAX_COINS:
        merged = f"{task}\n\n[추가 정보]\n" + "\n".join(
            u for u in user_facts if u != task
        )
        return {"ready": True, "enriched_task": merged.strip(), "question": ""}

    _require_quota(request, "collab", data.user_id, amount=1.0, action="intake")

    result = await call_ai_best(
        prompt, max_tokens=900, preferred_labels=[intake_model],
    )
    if result.success:
        m = re.search(r"\{[\s\S]*\}", result.content)
        if m:
            try:
                parsed = json.loads(m.group(0))
                if parsed.get("ready") and str(parsed.get("enriched_task", "")).strip():
                    return {
                        "ready": True,
                        "enriched_task": str(parsed["enriched_task"]).strip(),
                        "question": "",
                    }
                question = str(parsed.get("question", "")).strip()
                if question and not _is_repeat_question(question, asked_questions):
                    return {
                        "ready": False,
                        "question": question,
                        "enriched_task": "",
                        "intake_model": intake_model,
                    }
                if question and _is_repeat_question(question, asked_questions):
                    alt = _next_fallback_question()
                    if alt:
                        return {
                        "ready": False,
                        "question": alt,
                        "enriched_task": "",
                        "intake_model": intake_model,
                    }
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

    if user_turns >= len(fallback_questions):
        merged = f"{task}\n\n[추가 정보]\n" + "\n".join(
            u for u in user_facts if u != task
        )
        return {"ready": True, "enriched_task": merged.strip(), "question": ""}
    alt = _next_fallback_question()
    if alt:
        return {"ready": False, "question": alt, "enriched_task": ""}
    merged = f"{task}\n\n[추가 정보]\n" + "\n".join(u for u in user_facts if u != task)
    return {"ready": True, "enriched_task": merged.strip(), "question": ""}


@app.post("/collab/recommend")
async def collab_recommend(data: CollabRecommendRequest, request: Request) -> dict:
    """사용자 작업 설명을 작업 유형별 협업 알고리즘으로 변환한다."""
    await ensure_model_cache_fresh()
    task = data.task.strip()
    plan = build_collab_plan(task)

    type_names = ", ".join(r["type"] for r in COLLAB_TYPE_RULES)
    cls_prompt = (
        f"사용자 요청:\n{task}\n\n"
        f"가능한 작업 유형: {type_names}\n\n"
        "요청 내용에 가장 정확히 맞는 유형 이름 하나만 출력하세요. 다른 설명 없이 유형명만."
    )
    _require_quota(request, "collab", data.user_id, action="recommend_cls")
    cls_result = await call_ai_best(cls_prompt, max_tokens=80)
    if cls_result.success:
        picked = cls_result.content.strip().strip('"').strip("'")
        for rule in COLLAB_TYPE_RULES:
            if rule["type"] in picked or picked in rule["type"]:
                plan = build_collab_plan(task, forced_type=rule["type"])
                break

    prompt = (
        f"사용자 작업: {task}\n"
        f"분류된 작업 유형: {plan['work_type']}\n\n"
        "이 작업을 실제로 끝내기 위해 가장 중요한 주의점 3개와 "
        "처음 실행할 구체적 액션 3개를 한국어로 짧게 제안하세요."
    )

    _require_quota(request, "collab", data.user_id, action="recommend_tips")
    result = await call_ai_best(prompt, max_tokens=500)
    recommendation = result.content if result.success else (
        "AI 보완 코멘트 생성은 실패했지만, 아래 작업 유형별 협업 알고리즘은 바로 사용할 수 있습니다."
    )

    meta_lines = "\n".join(
        f"{i + 1}단계 {m['short']}({m['role']}): 후보 {', '.join(m['options'])}"
        for i, m in enumerate(COLLAB_STAGE_META)
    )
    stage_rec_prompt = (
        f"작업: {task}\n분류: {plan['work_type']}\n\n{meta_lines}\n\n"
        "각 단계(1~4)에 가장 적합한 AI 1개, 선택 이유(한국어 1~2문장), 대안 AI 2개를 정하세요.\n"
        'JSON만: {"stages":[{"index":1,"model":"Perplexity","reason":"...","alternatives":["xAI","Google"]}, ...]}'
    )
    _require_quota(request, "collab", data.user_id, action="recommend_stages")
    stage_rec_result = await call_ai_best(stage_rec_prompt, max_tokens=700)
    if stage_rec_result.success:
        import re

        m = re.search(r"\{[\s\S]*\}", stage_rec_result.content)
        if m:
            try:
                parsed = json.loads(m.group(0))
                recs = parsed.get("stages") or []
                if isinstance(recs, list) and recs:
                    merged_recs = []
                    new_models = []
                    for i, meta in enumerate(COLLAB_STAGE_META):
                        hit = next(
                            (r for r in recs if int(r.get("index", 0)) == i + 1),
                            None,
                        )
                        model = (hit or {}).get("model") or (
                            plan["stage_models"][i]
                            if i < len(plan["stage_models"])
                            else meta["default_model"]
                        )
                        if model not in meta["options"]:
                            model = meta["default_model"]
                        alts = [
                            a
                            for a in (hit or {}).get("alternatives") or []
                            if a in meta["options"] and a != model
                        ][:2]
                        if len(alts) < 2:
                            for opt in meta["options"]:
                                if opt != model and opt not in alts:
                                    alts.append(opt)
                                if len(alts) >= 2:
                                    break
                        merged_recs.append(
                            {
                                "index": i + 1,
                                "model": model,
                                "reason": (hit or {}).get("reason") or meta["hint"],
                                "alternatives": alts,
                            }
                        )
                        new_models.append(model)
                    plan["stage_recommendations"] = merged_recs
                    plan["stage_models"] = new_models
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

    return {
        "recommendation": recommendation,
        "plan": plan,
        "success": True,
        "ai_comment_success": result.success,
        "model_label": result.requested_label if result.success else None,
    }


def _collab_stage_prompt(data: CollabStageRequest) -> str:
    action_lines = "\n".join(f"- {action}" for action in data.actions) or "- (단계 알고리즘 없음)"
    tool_lines = ", ".join(data.tools) if data.tools else "추천 도구 없음"
    acceptance_lines = "\n".join(f"- {item}" for item in data.acceptance) or "- 단계 완료 후 다음 단계로 넘깁니다."
    stage_roles = ["조사·리서치", "구조·기획", "제작·초안", "검증·품질"]
    stage_role = stage_roles[data.stage_index % 4]
    rework_note = ""
    if data.is_rework and data.verification_feedback:
        rework_note = (
            f"\n\n[검증 AI 피드백 — 반드시 반영하여 이 단계 산출물을 수정]\n{data.verification_feedback}\n"
        )

    if data.stage_index == 3 and not data.is_rework:
        artifact = data.artifact_under_review or data.previous_notes or "(검토할 산출물 없음)"
        return (
            f"작업: {data.task}\n작업 유형: {data.work_type}\n"
            f"현재 단계: {data.stage_name} (4/4) — 역할: {stage_role}\n\n"
            f"검토 대상 작업물(제작 단계 산출물):\n{artifact}\n\n"
            f"이전 단계 요약:\n{data.previous_notes or '없음'}\n\n"
            f"통과 기준:\n{acceptance_lines}\n\n"
            "당신은 검증 AI입니다. **절대 작업물 전체를 재작성하지 마세요.**\n"
            "다음 형식으로 한국어 작성:\n"
            "1) 통과 기준별 평가 (통과/부분통과/미통과)\n"
            "2) 발견된 오류·누락·보완점 (심각도: 높음/중간/낮음)\n"
            "3) 단계별 수정 지시 — 조사 AI(1단계), 구조 AI(2단계), 제작 AI(3단계)에게 줄 구체적 지시\n"
            "4) 재작업이 필요한 단계 번호 (1=조사, 2=구조, 3=제작). 없으면 빈 배열\n\n"
            "마지막 줄에 반드시 JSON 한 줄:\n"
            '{"pass": true|false, "rework_stages": [1,2], "instructions": {"1":"...", "2":"...", "3":"..."}, "summary":"한줄요약"}'
        )

    stage_outputs = {
        0: (
            "조사·리서치 AI로서 다음을 수행하고 **실제 조사 결과**를 작성하세요:\n"
            "1) 작업 목표·제약·독자·성공 기준\n"
            "2) 수집한 근거·출처·팩트·유사사례 (가능하면 출처 표기)\n"
            "3) 핵심 인사이트·리스크·반례\n"
            "4) 구조 단계에 넘길 근거 패키지 (목록 형태)\n"
        ),
        1: (
            "구조·기획 AI로서 조사 결과를 바탕으로 **실행 가능한 구조**를 작성하세요:\n"
            "1) 목차/와이어프레임\n"
            "2) 섹션별 핵심 메시지·필수 포함 요소\n"
            "3) 논리 흐름·전환\n"
            "4) 제작 AI가 그대로 실행할 상세 지시문(프롬프트)\n"
        ),
        2: (
            "제작·초안 AI로서 구조·지시문에 따라 **실제 작업물 초안**을 작성하세요:\n"
            "1) 완성형 초안 본문 (형식·분량·톤 준수)\n"
            "2) 조사·구조 단계 반영 여부\n"
            "3) 검증 단계에서 점검할 최종 산출물\n"
        ),
    }
    role_instruction = stage_outputs.get(data.stage_index, "단계 결과를 작성하세요.")

    return (
        f"작업: {data.task}\n"
        f"작업 유형: {data.work_type}\n"
        f"현재 단계: {data.stage_name} ({data.stage_index + 1}/4) — 역할: {stage_role}\n"
        f"이 단계 알고리즘:\n{action_lines}\n"
        f"추천 도구: {tool_lines}\n"
        f"이전 단계 산출물:\n{data.previous_notes or '없음'}\n"
        f"{rework_note}\n"
        f"당신은 {stage_role} 전문 AI입니다.\n{role_instruction}\n"
        f"전체 통과 기준 참고:\n{acceptance_lines}"
    )


@app.post("/collab/run-stage")
async def collab_run_stage(data: CollabStageRequest, request: Request) -> dict:
    """협업 워크플로우의 특정 단계를 실행한다."""
    await ensure_model_cache_fresh()
    stage_model = (data.stage_model or "").strip() or COLLAB_STAGE_MODEL_LABELS[
        data.stage_index % len(COLLAB_STAGE_MODEL_LABELS)
    ]
    stage_roles = ["조사·리서치", "구조·기획", "제작·초안", "검증·품질"]
    stage_role = stage_roles[data.stage_index % 4]
    prompt = _collab_stage_prompt(data)
    max_tokens = 1200 if data.stage_index == 2 else (1000 if data.stage_index == 3 else 900)

    _require_quota(request, "collab", data.user_id, action="run_stage")
    result = await call_ai_best(prompt, max_tokens=max_tokens, preferred_labels=[stage_model])
    guidance = result.content if result.success else (
        f"{data.stage_name} 단계를 진행하세요.\n\n{prompt[:500]}..."
    )

    verify_meta = None
    if data.stage_index == 3 and not data.is_rework and guidance:
        import re
        m = re.search(r'\{[^{}]*"pass"[^{}]*\}', guidance)
        if m:
            try:
                verify_meta = json.loads(m.group(0))
            except json.JSONDecodeError:
                verify_meta = None

    return {
        "stage_index": data.stage_index,
        "stage_name": data.stage_name,
        "guidance": guidance,
        "model_label": result.requested_label if result.success else stage_model,
        "stage_role": stage_role,
        "success": True,
        "ai_success": result.success,
        "verify_meta": verify_meta,
    }


@app.post("/collab/followup-route")
async def collab_followup_route(data: CollabFollowupRequest, request: Request) -> dict:
    """추가 작업 요청을 분석해 재시작할 단계를 결정하거나(작업 중), 요청사항만 병합(작업 전)."""
    _require_quota(request, "collab", data.user_id, amount=1.0, action="followup")
    await ensure_model_cache_fresh()
    if data.pre_work:
        prompt = (
            f"원래 작업 요청:\n{data.original_task}\n\n"
            f"추가 요청:\n{data.task}\n\n"
            "아직 협업 작업을 시작하지 않았습니다. 추가 요청을 반영해 "
            "하나의 통합된 작업 설명(요청사항)을 작성하세요.\n"
            'JSON만: {"merged_task": "통합 요청사항", "reason": "무엇을 반영했는지 한국어 1~2문장"}'
        )
        result = await call_ai_best(prompt, max_tokens=400)
        merged = f"{data.original_task}\n\n[추가] {data.task}"
        reason = "추가 요청을 요청사항에 반영했습니다."
        if result.success:
            import re

            m = re.search(r"\{[\s\S]*?\}", result.content)
            if m:
                try:
                    parsed = json.loads(m.group(0))
                    merged = parsed.get("merged_task") or merged
                    reason = parsed.get("reason") or reason
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
        return {
            "pre_work": True,
            "merged_task": merged,
            "reason": reason,
            "success": True,
        }

    outputs_summary = "\n".join(
        f"[{i + 1}단계] {(t or '')[:400]}" for i, t in enumerate(data.stage_outputs[:3])
    )
    prompt = (
        f"원래 작업: {data.original_task}\n"
        f"작업 유형: {data.work_type}\n"
        f"추가 요청: {data.task}\n\n"
        f"현재 단계별 산출물 요약:\n{outputs_summary or '없음'}\n\n"
        "추가 요청을 반영하려면 어느 단계부터 다시 해야 하는지 판단하세요.\n"
        "1=조사, 2=구조, 3=제작. 논리·구조 수정이면 2, 사실·근거면 1, 문장·초안만이면 3.\n"
        "재작업 후 반드시 4단계 검증을 다시 거쳐야 합니다.\n"
        'JSON만 출력: {"start_stage": 1, "reason": "한국어 설명", "merged_task": "반영된 전체 작업 설명"}'
    )
    result = await call_ai_best(prompt, max_tokens=300)
    start_stage = 1
    reason = "추가 요청 반영"
    merged = f"{data.original_task}. 추가: {data.task}"
    if result.success:
        import re
        m = re.search(r"\{[^{}]+\}", result.content)
        if m:
            try:
                parsed = json.loads(m.group(0))
                start_stage = max(0, min(2, int(parsed.get("start_stage", 1)) - 1))
                reason = parsed.get("reason") or reason
                merged = parsed.get("merged_task") or merged
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
    return {"start_stage": start_stage, "reason": reason, "merged_task": merged, "success": True}


@app.get("/collab/templates")
def collab_templates() -> dict:
    return {"templates": COLLAB_QUICK_TEMPLATES}


@app.post("/debate/start")
async def debate_start(request: DebateRequest, http_request: Request) -> dict:
    await _require_debate_coin(http_request, request.user_id)
    await ensure_model_cache_fresh()
    session = DebateSession(topic=request.topic)
    store_debate_session(request.session_id, session)
    lock = get_debate_lock(request.session_id)

    async with lock:
        if request.user_input:
            append_pending_user_input(session, request.user_input, target_round=1)
        turns = await run_debate_step(session)
    store_debate_session(request.session_id, session)
    return debate_response(session, turns)


@app.post("/debate/continue")
async def debate_continue(request: DebateRequest, http_request: Request) -> dict:
    await ensure_model_cache_fresh()
    lock = get_debate_lock(request.session_id)

    if request.retry_speaker_index is not None:
        await _require_debate_coin(http_request, request.user_id)
        async with lock:
            session = load_debate_session(request.session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Debate session not found")
            turns = await retry_debate_speaker(session, request.retry_speaker_index)
            store_debate_session(request.session_id, session)
        return debate_response(session, turns)

    async with lock:
        session = load_debate_session(request.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Debate session not found")

        if request.user_input and len(session.current_round_turns) < 3:
            append_pending_user_input(session, request.user_input, target_round=session.round_number + 1)
            store_debate_session(request.session_id, session)
            return debate_response(session, [], queued=True)

        if request.user_input:
            append_pending_user_input(session, request.user_input, target_round=session.round_number + 1)
        await _require_debate_coin(http_request, request.user_id)
        turns = await run_debate_step(session)
        store_debate_session(request.session_id, session)

    return debate_response(session, turns)


def _parse_json_object(text: str) -> dict:
    if not text:
        return {}
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {}
    try:
        parsed = json.loads(m.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


@app.post("/compare/summary")
async def compare_summary(data: CompareSummaryRequest, request: Request) -> dict:
    await _require_compare_coin(request, data.user_id)
    await ensure_model_cache_fresh()
    parts = []
    for model, body in (data.responses or {}).items():
        clean = (body or "").strip()
        if clean:
            parts.append(f"[{model}]\n{clean[:2500]}")
    if not parts:
        raise HTTPException(status_code=400, detail="비교할 답변이 없습니다.")
    prompt = (
        f"질문: {data.message.strip()}\n\n"
        + "\n\n".join(parts)
        + "\n\n위 AI 답변들을 비교해 JSON만 출력하세요. "
        '{"common":"공통점 한 줄(40자 이내)","diff":"차이점 한 줄(40자 이내)",'
        '"pick":"추천 AI 모델명","line":"추천 답변 요약 한 줄(60자 이내)"}'
    )
    labels = ranked_labels()
    summary_label = labels[0] if labels else "GPT"
    result = await call_ai_best(prompt, max_tokens=350, preferred_labels=[summary_label])
    if not result.success:
        raise HTTPException(status_code=502, detail=result.error or USER_FACING_FAILURE_MSG)
    parsed = _parse_json_object(result.content)
    return {
        "common": str(parsed.get("common", "")).strip()[:80],
        "diff": str(parsed.get("diff", "")).strip()[:80],
        "pick": str(parsed.get("pick", summary_label)).strip()[:40],
        "line": str(parsed.get("line", "")).strip()[:120],
        "model": summary_label,
    }


@app.post("/debate/round-summary")
async def debate_round_summary(data: DebateRoundSummaryRequest, request: Request) -> dict:
    await _require_debate_coin(request, data.user_id)
    await ensure_model_cache_fresh()
    turns = data.turns or []
    if len(turns) < 2:
        raise HTTPException(status_code=400, detail="요약할 발언이 부족합니다.")
    turn_lines = []
    for turn in turns:
        idx = turn.get("speaker_index", "?")
        content = str(turn.get("content", "")).strip()
        if content:
            turn_lines.append(f"{idx}번: {content[:1200]}")
    prompt = (
        f"토론 주제: {data.topic.strip()}\n"
        f"{data.round_number}라운드 발언:\n"
        + "\n".join(turn_lines)
        + "\n\n위 토론을 한 장 요약 카드용으로 정리해 JSON만 출력: "
        '{"consensus":"합의·공통점 한 줄","dispute":"쟁점 한 줄","action":"다음 액션 한 줄",'
        '"summary":"전체 요약 3문장 이내"}'
    )
    labels = ranked_labels()
    summary_label = labels[0] if labels else "GPT"
    result = await call_ai_best(prompt, max_tokens=450, preferred_labels=[summary_label])
    if not result.success:
        raise HTTPException(status_code=502, detail=result.error or USER_FACING_FAILURE_MSG)
    parsed = _parse_json_object(result.content)
    summary_text = str(parsed.get("summary", "")).strip()
    if not summary_text:
        bits = [
            str(parsed.get("consensus", "")).strip(),
            str(parsed.get("dispute", "")).strip(),
            str(parsed.get("action", "")).strip(),
        ]
        summary_text = " · ".join(b for b in bits if b)
    return {
        "consensus": str(parsed.get("consensus", "")).strip()[:80],
        "dispute": str(parsed.get("dispute", "")).strip()[:80],
        "action": str(parsed.get("action", "")).strip()[:80],
        "summary": summary_text[:500],
        "model": summary_label,
    }


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


async def refresh_label_health(force: bool = False) -> None:
    global LABEL_HEALTH_REFRESHED_AT
    if (
        not force
        and LABEL_HEALTH
        and time.time() - LABEL_HEALTH_REFRESHED_AT < LABEL_HEALTH_TTL_SECONDS
    ):
        return

    await ensure_model_cache_fresh()
    semaphore = asyncio.Semaphore(HEALTHCHECK_CONCURRENCY)

    async def check_label(label: str) -> tuple[str, dict]:
        primary = MODEL_MAPPING.get(label)
        if not primary:
            return label, {"model": "", "status": "미설정", "ok": False}
        model_id, status = await check_model_health(semaphore, primary)
        return label, {"model": model_id, "status": status, "ok": status == "정상"}

    pairs = await asyncio.gather(*(check_label(label) for label in COMPANY_LABELS))
    LABEL_HEALTH.clear()
    for label, info in pairs:
        LABEL_HEALTH[label] = info
    LABEL_HEALTH_REFRESHED_AT = time.time()


def ranked_labels(preferred: list[str] | None = None) -> list[str]:
    preferred = preferred or []

    def sort_key(label: str) -> tuple:
        health = LABEL_HEALTH.get(label, {})
        ok_rank = 0 if health.get("ok") else 1
        pref_rank = preferred.index(label) if label in preferred else len(preferred) + 1
        return (ok_rank, pref_rank, label)

    ordered = list(dict.fromkeys([*preferred, *COMPANY_LABELS]))
    return sorted(ordered, key=sort_key)


async def call_ai_best(
    prompt: str,
    max_tokens: int | None = None,
    preferred_labels: list[str] | None = None,
) -> ModelCallResult:
    await refresh_label_health()
    last_result: ModelCallResult | None = None
    for label in ranked_labels(preferred_labels):
        result = await call_ai_model(label, prompt, max_tokens=max_tokens)
        last_result = result
        if result.success:
            return result
    if last_result is not None:
        return last_result
    fallback_label = preferred_labels[0] if preferred_labels else COMPANY_LABELS[0]
    return make_failed_result(
        requested_label=fallback_label,
        requested_model=MODEL_MAPPING.get(fallback_label, LAST_RESORT_MODEL),
        actual_model="",
        failed_candidates=[],
        error=USER_FACING_FAILURE_MSG,
    )


@app.get("/models/auto")
async def models_auto() -> dict:
    await refresh_label_health()
    ranked = ranked_labels()
    return {
        "ranked_labels": ranked,
        "best_label": ranked[0] if ranked else None,
        "health": LABEL_HEALTH,
        "refreshed_at": LABEL_HEALTH_REFRESHED_AT,
    }


AGENT_STEP_MAX_CANDIDATES = 6


def _resolve_agent_models() -> list[str]:
    """에이전트가 시도할 모델 목록: 유료 primary 우선, 실패 시 무료 폴백."""
    configured = os.environ.get("AGENT_MODEL", "").strip()
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    for label in COMPANY_LABELS:
        config = LABEL_PROVIDER_CONFIG[label]
        if config.official_model_id:
            candidates.append(config.official_model_id)
    candidates.extend(AGENT_PREFERRED_MODELS)
    candidates.extend(get_all_available_free_models())
    deduped = list(dict.fromkeys(c for c in candidates if c))
    if not deduped:
        deduped = [LAST_RESORT_MODEL]
    return deduped[:AGENT_STEP_MAX_CANDIDATES]


@app.post("/agent/step")
async def agent_step(request: AgentStepRequest, http_request: Request):
    """확장 프로그램이 사용자의 실제 탭에서 화면을 스캔해 보내면, 다음에 할
    액션 1개만 판단해 돌려주는 무상태 엔드포인트. Playwright를 전혀 쓰지 않는다.
    실제 클릭/입력은 확장(background.js)이 chrome.debugger로 수행한다."""
    task = request.task.strip()
    if not task:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "작업 내용을 입력해주세요."},
        )
    await _require_agent_coin(
        http_request,
        request.user_id,
        action="mission",
        detail=task[:200],
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
async def agent_task(request: AgentRequest, http_request: Request):
    query = request.query.strip()
    if not query:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "작업 내용을 입력해주세요."},
        )
    await _require_agent_coin(
        http_request,
        request.user_id,
        action="task",
        detail=query[:200],
    )

    if AGENT_LOCK.locked():
        return JSONResponse(
            status_code=429,
            content={"status": "error", "message": "이미 다른 에이전트 작업이 실행 중입니다. 잠시 후 다시 시도해주세요."},
        )

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
async def agent_ask(request: AgentAskRequest, http_request: Request):
    """브라우저 없이 순수 LLM으로 임무를 수행한다.
    /agent/task(Playwright)가 실패할 때의 폴백이자, 단순 분석/답변 임무에 사용."""
    query = request.query.strip()
    if not query:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "임무를 입력해주세요."},
        )
    await _require_agent_coin(
        http_request,
        request.user_id,
        action="ask",
        detail=query[:200],
    )

    await ensure_model_cache_fresh()
    models = _resolve_agent_models()
    headers = build_openrouter_headers()

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
    return FileResponse(
        index_path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/install")
def serve_install() -> FileResponse:
    return FileResponse(os.path.join(BASE_DIR, "install.html"))


@app.get("/google3cf35a4abfa671e1.html")
def serve_google_site_verification() -> FileResponse:
    path = os.path.join(BASE_DIR, "google3cf35a4abfa671e1.html")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="text/html")


def _read_version_file(path: str, pattern: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        m = re.search(pattern, text)
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


@app.get("/system/info")
def system_info() -> dict:
    ext_ver = _read_version_file(
        os.path.join(BASE_DIR, "extension", "manifest.json"),
        r'"version"\s*:\s*"([^"]+)"',
    )
    apk_ver = _read_version_file(
        os.path.join(BASE_DIR, "android-agent", "app", "build.gradle"),
        r'versionName\s+"([^"]+)"',
    )
    return {
        "server": "nasaroai",
        "deploy_ref": os.environ.get("RENDER_GIT_COMMIT", os.environ.get("GIT_COMMIT", "local")),
        "extension_version": ext_ver,
        "apk_version": apk_ver,
        "guide_path": "/guide",
        "auto_ai_label": ranked_labels()[0] if LABEL_HEALTH else None,
    }


@app.get("/quota")
def quota_status(request: Request, device_id: str = "") -> dict:
    subject, user = _resolve_subject(request, device_id)
    snap = get_quota_snapshot(subject)
    return {"subject": subject, "logged_in": user is not None, **snap}


@app.post("/device/register")
def device_register(body: DeviceRegisterRequest) -> dict:
    """Stable guest device id — fingerprint prevents quota reset by clearing storage."""
    dev_id = resolve_device_id(body.fingerprint, body.device_id)
    return {"device_id": dev_id, "ok": True}


@app.post("/presence")
def touch_presence(body: PresenceRequest, request: Request) -> dict:
    """클라이언트 하트비트 — 관리자 콘솔 실시간 접속 표시용."""
    user = get_user_by_token(_bearer_token(request))
    dev = (body.device_id or request.headers.get("X-Device-Id") or "").strip()
    if dev:
        touch_device_presence(dev, _platform(request))
    return {"ok": True, "logged_in": user is not None}


@app.post("/auth/signup")
def auth_signup_route(body: AuthSignupRequest) -> dict:
    try:
        return auth_signup_fn(body.username, body.email, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/auth/login")
def auth_login_route(body: AuthLoginRequest) -> dict:
    try:
        return auth_login_fn(body.username, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/auth/logout")
def auth_logout_route(request: Request) -> dict:
    token = _bearer_token(request)
    if token:
        auth_logout_fn(token)
    return {"success": True}


@app.get("/auth/me")
def auth_me(request: Request) -> dict:
    user = get_user_by_token(_bearer_token(request))
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return {"user": user}


@app.get("/user/data")
def user_data_get(request: Request) -> dict:
    user = get_user_by_token(_bearer_token(request))
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return {"data": get_user_data(user["id"])}


@app.post("/user/sync")
def user_data_sync(body: UserSyncRequest, request: Request) -> dict:
    user = get_user_by_token(_bearer_token(request))
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    merged = merge_user_data(user["id"], body.model_dump(exclude_none=True))
    return {"success": True, "data": merged}


@app.post("/share/create")
def share_create(body: ShareCreateRequest) -> dict:
    if not body.payload:
        raise HTTPException(status_code=400, detail="공유할 내용이 없습니다.")
    kind = (body.kind or "compare").strip()[:32]
    share_id = create_public_share(kind, body.title, body.payload)
    return {"id": share_id, "url": f"/?share={share_id}"}


@app.get("/share/{share_id}")
def share_read(share_id: str) -> dict:
    item = get_public_share(share_id)
    if not item:
        raise HTTPException(status_code=404, detail="공유 링크를 찾을 수 없습니다.")
    return item


@app.get("/guide")
def serve_guide() -> FileResponse:
    path = os.path.join(BASE_DIR, "guide.html")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="가이드 문서를 찾을 수 없습니다.")
    return FileResponse(
        path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/admin")
def serve_admin() -> FileResponse:
    path = os.path.join(BASE_DIR, "admin.html")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="관리자 페이지를 찾을 수 없습니다.")
    return FileResponse(
        path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.post("/admin/login")
def admin_login(body: AdminLoginRequest) -> dict:
    if not verify_admin_password(body.password):
        raise HTTPException(status_code=401, detail="관리자 비밀번호가 올바르지 않습니다.")
    token = create_admin_session()
    return {"token": token}


@app.get("/admin/dashboard")
def admin_dashboard(request: Request) -> dict:
    _require_admin(request)
    return get_admin_dashboard()


@app.get("/admin/users/search")
def admin_search_users(request: Request, q: str = "", limit: int = 40) -> dict:
    _require_admin(request)
    return {"users": search_users_admin(q, limit=limit)}


@app.get("/admin/users/{user_id}")
def admin_user_detail(user_id: int, request: Request) -> dict:
    _require_admin(request)
    try:
        return get_user_admin_detail(user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get("/admin/guests")
def admin_guests(request: Request, limit: int = 50) -> dict:
    _require_admin(request)
    return {"guests": list_guest_devices(limit=limit)}


@app.get("/admin/activity")
def admin_activity(
    request: Request,
    user_id: int | None = None,
    device_id: str | None = None,
    feature: str | None = None,
    q: str | None = None,
    offset: int = 0,
    limit: int = 200,
) -> dict:
    _require_admin(request)
    limit = max(1, min(5000, limit))
    offset = max(0, offset)
    activity = get_activity_log(
        user_id=user_id,
        device_id=device_id,
        feature=feature,
        q=q,
        limit=limit,
        offset=offset,
    )
    total = count_activity_log(
        user_id=user_id,
        device_id=device_id,
        feature=feature,
        q=q,
    )
    return {"activity": activity, "total": total, "limit": limit, "offset": offset}


@app.get("/admin/activity/{activity_id}")
def admin_activity_detail(activity_id: int, request: Request) -> dict:
    _require_admin(request)
    row = get_activity_by_id(activity_id)
    if not row:
        raise HTTPException(status_code=404, detail="활동 기록을 찾을 수 없습니다.")
    return {"activity": row}


@app.delete("/admin/activity")
def admin_activity_delete(body: AdminActivityDeleteRequest, request: Request) -> dict:
    _require_admin(request)
    if body.all:
        deleted = delete_activity_records(delete_all=True)
    elif body.ids:
        deleted = delete_activity_records(ids=body.ids)
    else:
        raise HTTPException(status_code=400, detail="삭제할 항목을 선택하세요.")
    return {"success": True, "deleted": deleted}


@app.get("/admin/settings")
def admin_settings_get(request: Request) -> dict:
    _require_admin(request)
    return {"activity_retention_days": get_activity_retention_days()}


@app.post("/admin/settings")
def admin_settings_save(body: AdminSettingsRequest, request: Request) -> dict:
    _require_admin(request)
    days = max(0, min(3650, int(body.activity_retention_days)))
    set_admin_setting("activity_retention_days", str(days))
    purged = purge_expired_activity()
    return {"success": True, "activity_retention_days": days, "purged": purged}


@app.post("/user/activity")
def user_activity_log(body: UserActivityLogRequest, request: Request) -> dict:
    user = get_user_by_token(_bearer_token(request))
    subject, resolved_user = _resolve_subject(request, body.device_id)
    uid = user["id"] if user else (resolved_user["id"] if resolved_user else None)
    if body.privacy and not user:
        raise HTTPException(status_code=401, detail="프라이버시 모드는 로그인 후 이용할 수 있습니다.")
    is_secret = _privacy_from_request(request, user, body.privacy)
    feature = (body.feature or "compare").strip()[:32]
    dev = (body.device_id or request.headers.get("X-Device-Id") or "").strip()
    row_id = log_user_activity_detail(
        subject,
        feature,
        user_id=uid,
        device_id=dev,
        platform=_platform(request),
        action=(body.action or feature)[:64],
        question=body.question,
        answer=body.answer,
        is_secret=is_secret,
    )
    return {"success": True, "id": row_id, "secret": is_secret}


@app.get("/admin/support")
def admin_support_list(request: Request, status: str | None = None) -> dict:
    _require_admin(request)
    return {"inquiries": list_support_inquiries(status=status)}


@app.get("/admin/support/{inquiry_id}")
def admin_support_thread(inquiry_id: int, request: Request) -> dict:
    _require_admin(request)
    thread = get_support_thread(inquiry_id)
    if not thread:
        raise HTTPException(status_code=404, detail="문의를 찾을 수 없습니다.")
    return thread


@app.post("/admin/support/{inquiry_id}/reply")
def admin_support_reply(inquiry_id: int, body: SupportReplyRequest, request: Request) -> dict:
    _require_admin(request)
    try:
        add_support_reply(inquiry_id, body.message, from_admin=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True}


@app.delete("/admin/support/{inquiry_id}")
def admin_support_delete(inquiry_id: int, request: Request) -> dict:
    _require_admin(request)
    try:
        delete_support_inquiry_admin(inquiry_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"success": True}


@app.get("/admin/quota")
def admin_quota_lookup(
    request: Request,
    subject: str = "",
    user_id: int | None = None,
    device_id: str = "",
) -> dict:
    _require_admin(request)
    subj = (subject or "").strip()
    if not subj:
        if user_id is not None:
            subj = f"user:{user_id}"
        elif device_id.strip():
            subj = f"device:{device_id.strip()}"
    if not subj:
        raise HTTPException(status_code=400, detail="subject, user_id, or device_id required")
    snap = get_quota_snapshot(subj)
    return {"subject": subj, **snap}


@app.post("/admin/quota/adjust")
def admin_quota_adjust(body: AdminQuotaAdjustRequest, request: Request) -> dict:
    _require_admin(request)
    try:
        return admin_adjust_quota(body.subject, body.feature, body.delta)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/admin/quota/limit")
def admin_quota_set_limit(body: AdminQuotaLimitRequest, request: Request) -> dict:
    _require_admin(request)
    try:
        return admin_set_quota_limit(body.subject, body.feature, body.daily_limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/admin/ban")
def admin_ban_subject(body: AdminBanRequest, request: Request) -> dict:
    _require_admin(request)
    try:
        set_subject_ban(body.subject, body.banned, body.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, "subject": body.subject.strip(), "banned": body.banned}


@app.post("/support/inquiry")
def user_support_inquiry(body: SupportInquiryRequest, request: Request) -> dict:
    user = get_user_by_token(_bearer_token(request))
    if not user:
        raise HTTPException(status_code=401, detail="문의는 로그인 후 이용할 수 있습니다.")
    dev = (body.device_id or request.headers.get("X-Device-Id") or "").strip()
    username = user.get("username") or user.get("email") or ""
    user_id = user["id"]
    try:
        row = create_support_inquiry(
            body.message,
            user_id=user_id,
            device_id=dev,
            platform=_platform(request),
            username=username,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    subject, _ = _resolve_subject(request, dev)
    log_activity(
        subject,
        "support",
        user_id=user_id,
        device_id=dev,
        platform=_platform(request),
        action="inquiry",
        detail=body.message[:120],
    )
    return row


@app.get("/support/inquiries")
def user_support_list(request: Request) -> dict:
    user = get_user_by_token(_bearer_token(request))
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return {"inquiries": list_user_support_inquiries(int(user["id"]))}


@app.delete("/support/inquiry/{inquiry_id}")
def user_support_delete(inquiry_id: int, request: Request) -> dict:
    user = get_user_by_token(_bearer_token(request))
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    try:
        delete_support_inquiry(inquiry_id, int(user["id"]))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True}


@app.post("/admin/logout")
def admin_logout_route(request: Request) -> dict:
    token = _admin_bearer(request)
    if token:
        admin_logout(token)
    return {"success": True}


@app.get("/manifest.json")
def serve_manifest() -> FileResponse:
    path = os.path.join(BASE_DIR, "manifest.json")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/manifest+json")


@app.get("/extension-update")
def extension_update(request: Request):
    """Chrome 확장 자동 업데이트 매니페스트 (update_url)"""
    raw = request.query_params.get("x", "")
    ext_id = ""
    for part in raw.replace("%3D", "=").replace("%26", "&").split("&"):
        if part.startswith("id="):
            ext_id = part[3:]
    if not ext_id:
        ext_id = "nasaroai-agent"
    ext_ver = _read_version_file(
        os.path.join(BASE_DIR, "extension", "manifest.json"),
        r'"version"\s*:\s*"([^"]+)"',
    )
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>"
        f"<app appid='{ext_id}'>"
        "<updatecheck"
        " status='ok'"
        f" version='{ext_ver}'"
        " prodversionmin='88.0'"
        " codebase='https://nasaroai.onrender.com/static/nasaroai-extension.zip'"
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
    uvicorn.run(app, host="0.0.0.0", port=port, timeout_keep_alive=120)
