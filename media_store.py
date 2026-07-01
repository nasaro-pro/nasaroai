"""Image/video/audio generation via OpenRouter and fal.ai."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("nasaroai")

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_IMAGES_URL = "https://openrouter.ai/api/v1/images"
OPENROUTER_SPEECH_URL = "https://openrouter.ai/api/v1/audio/speech"
FAL_QUEUE_URL = "https://queue.fal.run"


@dataclass(frozen=True)
class MediaProviderConfig:
    label: str
    modality: str  # image | video | audio
    provider: str  # openrouter | openrouter_image | openrouter_speech | fal
    model_id: str
    extra_params: dict = field(default_factory=dict)


FAL_SERVICE_UNAVAILABLE_MSG = "현재 해당 기능은 서비스 준비 중입니다. 잠시만 기다려주세요!"


class FalServiceUnavailableError(Exception):
    """Raised when fal.ai provider is requested but not enabled for deployment."""

    def __init__(self, message: str = FAL_SERVICE_UNAVAILABLE_MSG) -> None:
        super().__init__(message)
        self.message = message


MEDIA_PROVIDER_CONFIG: dict[str, MediaProviderConfig] = {
    "Seedream 4.5": MediaProviderConfig(
        "Seedream 4.5", "image", "openrouter_image", "bytedance-seed/seedream-4.5"
    ),
    "Grok Imagine": MediaProviderConfig(
        "Grok Imagine", "image", "openrouter_image", "x-ai/grok-imagine-image"
    ),
    "Grok Imagine Pro": MediaProviderConfig(
        "Grok Imagine Pro", "image", "openrouter_image", "x-ai/grok-imagine-image-pro"
    ),
    "Nano Banana Pro": MediaProviderConfig(
        "Nano Banana Pro", "image", "openrouter_image", "google/gemini-3-pro-image-preview"
    ),
    "Flux Pro": MediaProviderConfig(
        "Flux Pro", "image", "openrouter_image", "black-forest-labs/flux-pro-1.1"
    ),
    "Veo 3.1 Fast": MediaProviderConfig(
        "Veo 3.1 Fast", "video", "fal", "fal-ai/veo3.1/fast/image-to-video",
        {"duration": "5s"},
    ),
    "Veo 3.1 Standard": MediaProviderConfig(
        "Veo 3.1 Standard", "video", "fal", "fal-ai/veo3.1/standard/image-to-video",
        {"duration": "5s"},
    ),
    "Kling 3.0 Standard": MediaProviderConfig(
        "Kling 3.0 Standard", "video", "fal", "fal-ai/kling-video/v3/standard/text-to-video",
        {"duration": "5"},
    ),
    "Sora 2 Pro": MediaProviderConfig(
        "Sora 2 Pro", "video", "fal", "fal-ai/sora/v2/pro/text-to-video",
        {"duration": "5"},
    ),
    "GPT-4o Mini TTS": MediaProviderConfig(
        "GPT-4o Mini TTS", "audio", "openrouter_speech",
        "openai/gpt-4o-mini-tts-2025-12-15",
        {"voice": "alloy", "response_format": "mp3"},
    ),
    "Gemini Flash TTS": MediaProviderConfig(
        "Gemini Flash TTS", "audio", "openrouter_speech",
        "google/gemini-3.1-flash-tts-preview",
        {"voice": "Kore", "response_format": "mp3"},
    ),
    "Voxtral Mini TTS": MediaProviderConfig(
        "Voxtral Mini TTS", "audio", "openrouter_speech",
        "mistralai/voxtral-mini-tts-2603",
        {"voice": "default", "response_format": "mp3"},
    ),
}


def get_fal_api_key() -> str:
    return (
        os.environ.get("FAL_KEY", "").strip()
        or os.environ.get("FAL_API_KEY", "").strip()
    )


def missing_fal_key_message() -> str:
    return FAL_SERVICE_UNAVAILABLE_MSG


def is_fal_media_label(label: str) -> bool:
    cfg = MEDIA_PROVIDER_CONFIG.get((label or "").strip())
    return bool(cfg and cfg.provider == "fal")


def media_modality_for_label(label: str) -> str:
    cfg = MEDIA_PROVIDER_CONFIG.get(label)
    return cfg.modality if cfg else "chat"


def _extract_image_from_images_api(data: dict) -> str:
    items = data.get("data") or []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or ""
        if url:
            return str(url)
        b64 = item.get("b64_json") or item.get("b64") or ""
        if b64:
            return f"data:image/png;base64,{b64}"
    return ""


def _extract_image_url_from_chat(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    images = msg.get("images") or []
    for img in images:
        if isinstance(img, dict):
            url = (img.get("image_url") or {}).get("url") or img.get("url") or ""
            if url:
                return str(url)
    content = msg.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url") or ""
                if url:
                    return str(url)
    if isinstance(content, str) and content.startswith("http"):
        return content.strip()
    return ""


async def generate_image_openrouter(
    model_id: str,
    prompt: str,
    headers: dict[str, str],
    image_url: str = "",
    aspect_ratio: str = "1:1",
) -> str:
    payload: dict[str, Any] = {
        "model": model_id,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio or "1:1",
        "n": 1,
    }
    if image_url:
        payload["input_references"] = [{"image_url": image_url}]
    async with httpx.AsyncClient(timeout=180) as client:
        res = await client.post(OPENROUTER_IMAGES_URL, headers=headers, json=payload)
    if res.status_code == 200:
        url = _extract_image_from_images_api(res.json())
        if url:
            return url
    # Fallback: chat completions image modality (legacy models)
    user_content: Any = prompt
    if image_url:
        user_content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]
    chat_payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": user_content}],
        "modalities": ["image", "text"],
    }
    async with httpx.AsyncClient(timeout=180) as client:
        res2 = await client.post(OPENROUTER_CHAT_URL, headers=headers, json=chat_payload)
    if res2.status_code != 200:
        detail = res.text[:200] if res.status_code != 200 else res2.text[:200]
        raise RuntimeError(f"OpenRouter image HTTP {res.status_code}/{res2.status_code}: {detail}")
    url2 = _extract_image_url_from_chat(res2.json())
    if not url2:
        raise RuntimeError("OpenRouter 응답에서 이미지 URL을 찾지 못했습니다.")
    return url2


async def generate_audio_openrouter(
    model_id: str,
    prompt: str,
    headers: dict[str, str],
    *,
    voice: str = "alloy",
    response_format: str = "mp3",
) -> str:
    payload = {
        "model": model_id,
        "input": prompt,
        "voice": voice or "alloy",
        "response_format": response_format or "mp3",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        res = await client.post(OPENROUTER_SPEECH_URL, headers=headers, json=payload)
    if res.status_code != 200:
        raise RuntimeError(f"OpenRouter TTS HTTP {res.status_code}: {res.text[:300]}")
    ctype = res.headers.get("content-type", "")
    if "json" in ctype:
        data = res.json()
        b64 = ""
        if isinstance(data.get("data"), list) and data["data"]:
            b64 = data["data"][0].get("b64_json") or data["data"][0].get("audio") or ""
        if b64:
            return f"data:audio/mp3;base64,{b64}"
        raise RuntimeError("OpenRouter TTS JSON 응답에서 오디오를 찾지 못했습니다.")
    b64 = base64.b64encode(res.content).decode("ascii")
    fmt = "mp3" if response_format == "mp3" else "wav"
    return f"data:audio/{fmt};base64,{b64}"


async def _fal_submit(model_id: str, payload: dict) -> tuple[str, str]:
    key = get_fal_api_key()
    if not key:
        raise RuntimeError(missing_fal_key_message())
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    url = f"{FAL_QUEUE_URL}/{model_id}"
    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(url, headers=headers, json=payload)
    if res.status_code not in (200, 202):
        raise RuntimeError(f"fal.ai HTTP {res.status_code}: {res.text[:300]}")
    data = res.json()
    req_id = data.get("request_id") or data.get("gateway_request_id") or ""
    status_url = data.get("status_url") or (f"{FAL_QUEUE_URL}/{model_id}/requests/{req_id}/status" if req_id else "")
    if not status_url:
        result_url = _extract_fal_media_url(data)
        if result_url:
            return req_id or str(uuid.uuid4()), result_url
        raise RuntimeError("fal.ai 요청 ID를 받지 못했습니다.")
    return req_id or str(uuid.uuid4()), status_url


def _extract_fal_media_url(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("video", "image", "images", "output"):
        val = data.get(key)
        if isinstance(val, dict):
            u = val.get("url") or val.get("video_url") or val.get("image_url")
            if u:
                return str(u)
        if isinstance(val, list) and val:
            first = val[0]
            if isinstance(first, dict):
                u = first.get("url") or first.get("video_url") or first.get("image_url")
                if u:
                    return str(u)
            if isinstance(first, str) and first.startswith("http"):
                return first
        if isinstance(val, str) and val.startswith("http"):
            return val
    resp = data.get("response") or data.get("payload")
    if isinstance(resp, dict):
        return _extract_fal_media_url(resp)
    return ""


async def poll_fal_status(status_url: str, *, max_wait: int = 600) -> str:
    key = get_fal_api_key()
    headers = {"Authorization": f"Key {key}"}
    elapsed = 0
    interval = 3
    async with httpx.AsyncClient(timeout=60) as client:
        while elapsed < max_wait:
            res = await client.get(status_url, headers=headers)
            if res.status_code != 200:
                raise RuntimeError(f"fal poll HTTP {res.status_code}")
            data = res.json()
            status = (data.get("status") or "").upper()
            if status in ("COMPLETED", "OK", "SUCCESS"):
                url = _extract_fal_media_url(data)
                if url:
                    return url
                result_url = data.get("response_url") or data.get("result_url")
                if result_url:
                    r2 = await client.get(str(result_url), headers=headers)
                    if r2.status_code == 200:
                        url2 = _extract_fal_media_url(r2.json())
                        if url2:
                            return url2
                raise RuntimeError("fal.ai 완료 응답에서 미디어 URL을 찾지 못했습니다.")
            if status in ("FAILED", "ERROR"):
                raise RuntimeError(data.get("error") or data.get("detail") or "fal.ai 생성 실패")
            await asyncio.sleep(interval)
            elapsed += interval
    raise RuntimeError("fal.ai 생성 시간 초과")


async def generate_video_fal(model_id: str, prompt: str, image_url: str = "", extra: dict | None = None) -> str:
    payload: dict[str, Any] = {"prompt": prompt}
    if extra:
        payload.update(extra)
    if image_url:
        payload["image_url"] = image_url
    _req_id, status_or_url = await _fal_submit(model_id, payload)
    if status_or_url.startswith("http") and "/status" not in status_or_url and "queue" not in status_or_url:
        return status_or_url
    return await poll_fal_status(status_or_url, max_wait=900)


async def run_media_generation(
    label: str,
    prompt: str,
    *,
    openrouter_headers: dict[str, str] | None = None,
    image_url: str = "",
    aspect_ratio: str = "1:1",
) -> str:
    cfg = MEDIA_PROVIDER_CONFIG.get(label)
    if not cfg:
        raise ValueError(f"Unknown media label: {label}")
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("프롬프트를 입력하세요.")
    if cfg.provider in ("openrouter", "openrouter_image"):
        if not openrouter_headers:
            raise RuntimeError("OpenRouter API key required")
        return await generate_image_openrouter(
            cfg.model_id, prompt, openrouter_headers,
            image_url=image_url, aspect_ratio=aspect_ratio,
        )
    if cfg.provider == "openrouter_speech":
        if not openrouter_headers:
            raise RuntimeError("OpenRouter API key required")
        extra = cfg.extra_params or {}
        return await generate_audio_openrouter(
            cfg.model_id, prompt, openrouter_headers,
            voice=str(extra.get("voice", "alloy")),
            response_format=str(extra.get("response_format", "mp3")),
        )
    if cfg.provider == "fal":
        raise FalServiceUnavailableError()
    raise RuntimeError(f"Unsupported provider: {cfg.provider}")
