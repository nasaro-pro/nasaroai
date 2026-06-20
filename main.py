from fastapi.responses import FileResponse
import asyncio
import hashlib
import json
import os
import random
from typing import Dict

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="ArenaX v2 Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

QUERY_CACHE: Dict[str, Dict] = {}
DEBATE_SESSIONS: Dict[str, Dict] = {}

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "HTTP-Referer": "https://arenax.com",
    "X-Title": "ArenaX",
    "Content-Type": "application/json",
}

# 서버 시작 시 키 로딩 상태를 바로 확인할 수 있도록 로그 출력
print(
    f"[BOOT] OPENROUTER_API_KEY loaded: {bool(OPENROUTER_API_KEY)}, "
    f"length: {len(OPENROUTER_API_KEY)}, "
    f"prefix: {OPENROUTER_API_KEY[:8] if OPENROUTER_API_KEY else 'N/A'}"
)

# ============================================================
# 페르소나 설계 원칙
# ------------------------------------------------------------
# 억지로 캐릭터(성격, 말투, 밈 등)를 부여하지 않습니다.
# 각 회사/모델이 실제로 강점을 보이는 답변 방식에 집중하도록만 안내합니다.
# 톤은 자연스러운 존댓말로 통일하고, 강조하는 "관점"만 다르게 둡니다.
# ============================================================
PERSONAS = {
    "OpenAI": "당신은 범용적이고 체계적인 설명에 강합니다. 질문의 핵심을 구조적으로 정리하고, 단계적으로 이해하기 쉽게 설명하는 데 집중해서 대답하세요.",
    "Anthropic": "당신은 신중하고 다각도의 분석에 강합니다. 한 가지 결론으로 단정하기보다, 여러 관점과 trade-off를 균형 있게 짚어가며 대답하세요.",
    "Google": "당신은 실용적이고 최신 정보에 기반한 답변에 강합니다. 핵심만 간결하게 추리고, 실제로 적용 가능한 정보 위주로 대답하세요.",
    "xAI": "당신은 직설적이고 가감 없는 분석에 강합니다. 돌려 말하지 않고 핵심 의견을 명확하게, 최신 맥락을 반영해 대답하세요.",
    "Perplexity": "당신은 사실 검증과 근거 제시에 강합니다. 가능한 한 구체적인 근거나 출처가 될 만한 정보를 함께 제시하며 신뢰도 높게 대답하세요.",
}

# ============================================================
# 모델 매핑 안내 (2026-06-20 기준 OpenRouter 무료 카탈로그 확인)
# ------------------------------------------------------------
#  - OpenAI   : gpt-oss-120b → OpenAI가 직접 공개한 오픈웨이트 모델 (실제 일치)
#  - Google   : Gemma 4 31B → Google DeepMind가 직접 공개한 오픈소스 모델 (실제 일치)
#  - xAI      : Grok 4 Fast(free) → xAI 모델이지만 한시적 무료 프로모션이라
#               언제든 유료로 전환되거나 사라질 수 있음 (실패 시 FALLBACK_MODEL로 자동 대체)
#  - Anthropic: Claude는 OpenRouter에 무료 버전이 전혀 없음 → 대체 오픈소스 모델 사용
#  - Perplexity: Sonar 계열은 전부 유료 → 대체 오픈소스 모델 사용
#
# UI에는 회사 라벨을 그대로 두되, Anthropic/Perplexity 자리는 실제로는
# 해당 회사 모델이 아니라는 점을 IS_REAL_COMPANY_MODEL로 프론트에 전달합니다.
#
# 모델 ID는 OpenRouter 정책에 따라 자주 바뀝니다. 문제가 생기면
# https://openrouter.ai/models 에서 Price=Free로 필터링해 최신 ID로 교체하세요.
# (404가 뜨면 ID가 죽은 것, 429가 뜨면 한도 초과)
# ============================================================

MODEL_MAPPING = {
    "OpenAI": "openai/gpt-oss-120b:free",
    "Anthropic": "nvidia/nemotron-3-super-120b-a12b:free",
    "Google": "google/gemma-4-31b-it:free",
    "xAI": "x-ai/grok-4-fast:free",
    "Perplexity": "deepseek/deepseek-r1:free",
}

# xAI의 Grok 무료 슬러그가 프로모션 종료 등으로 죽었을 때 자동으로 전환할 모델
FALLBACK_MODEL = "openai/gpt-oss-20b:free"

# 실제로 해당 회사가 만든 모델인지 여부 (프론트엔드 안내 문구용)
IS_REAL_COMPANY_MODEL = {
    "OpenAI": True,
    "Anthropic": False,
    "Google": True,
    "xAI": True,
    "Perplexity": False,
}

class CompareRequest(BaseModel):
    message: str
    model_name: str

class DebateRequest(BaseModel):
    session_id: str
    topic: str
    action: str

def hash_message(message: str) -> str:
    return hashlib.md5(message.encode()).hexdigest()

async def _post_with_fallback(client: httpx.AsyncClient, payload: dict, model_name: str):
    """OpenRouter에 요청을 보내고, 429면 잠깐 대기 후 재시도, 모델 ID가 죽은 경우(404/400)면 폴백 모델로 재시도."""
    response = await client.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=HEADERS,
        json=payload,
    )

    if response.status_code == 429:
        retry_after = response.headers.get("retry-after")
        wait_seconds = float(retry_after) if retry_after else 2.0
        print(f"[RATE LIMIT] label={model_name} model={payload['model']} waiting {wait_seconds}s")
        await asyncio.sleep(min(wait_seconds, 5.0))
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=HEADERS,
            json=payload,
        )

    if response.status_code in (400, 404) and payload["model"] != FALLBACK_MODEL:
        body = response.text
        print(
            f"[MODEL FALLBACK] label={model_name} "
            f"{payload['model']} -> {FALLBACK_MODEL} "
            f"(status={response.status_code}, body={body[:300]})"
        )
        payload = {**payload, "model": FALLBACK_MODEL}
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=HEADERS,
            json=payload,
        )
    return response

async def call_ai_model(model_name: str, prompt: str, persona: str) -> str:
    payload = {
        "model": MODEL_MAPPING.get(model_name, "openai/gpt-oss-120b:free"),
        "messages": [
            {"role": "system", "content": persona},
            {"role": "user", "content": prompt}
        ],
        "stream": False
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await _post_with_fallback(client, payload, model_name)
            if response.status_code == 200:
                data = response.json()
                return data.get("choices", [{}])[0].get("message", {}).get("content", "[응답 없음]")
            else:
                body = response.text
                print(f"[CALL ERROR] model={model_name} status={response.status_code} body={body[:500]}")
                return f"[API 오류 {response.status_code}]"
    except Exception as e:
        print(f"[CALL EXCEPTION] model={model_name} error={str(e)}")
        return f"[통신 오류: {str(e)}]"

@app.post("/compare/stream")
async def stream_compare(data: CompareRequest):
    if data.model_name not in MODEL_MAPPING:
        raise HTTPException(status_code=400, detail="Invalid model_name")

    model_name = data.model_name
    base_payload = {
        "model": MODEL_MAPPING[model_name],
        "messages": [
            {"role": "system", "content": PERSONAS[model_name]},
            {"role": "user", "content": data.message},
        ],
        "stream": True,
    }

    async def generate():
        error_event = (
            f"data: {json.dumps({'model': model_name, 'error': 'API 한도 초과 또는 오류가 발생했습니다.'}, ensure_ascii=False)}\n\n"
        )

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                current_payload = base_payload
                attempted_fallback = False
                attempted_retry_429 = False

                while True:
                    async with client.stream(
                        "POST",
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers=HEADERS,
                        json=current_payload,
                    ) as response:
                        if response.status_code != 200:
                            body = await response.aread()
                            print(
                                f"[STREAM ERROR] label={model_name} "
                                f"requested_model={current_payload['model']} "
                                f"status={response.status_code} body={body[:500]}"
                            )
                            # 429(레이트리밋): 잠깐 대기 후 같은 모델로 1회 재시도
                            if response.status_code == 429 and not attempted_retry_429:
                                attempted_retry_429 = True
                                retry_after = response.headers.get("retry-after")
                                wait_seconds = float(retry_after) if retry_after else 2.0
                                await asyncio.sleep(min(wait_seconds, 5.0))
                                continue
                            # 모델 ID가 죽었을 가능성(404/400) → 폴백 모델로 1회만 재시도
                            if response.status_code in (400, 404) and not attempted_fallback and current_payload["model"] != FALLBACK_MODEL:
                                attempted_fallback = True
                                current_payload = {**current_payload, "model": FALLBACK_MODEL}
                                continue
                            yield error_event
                            return

                        had_error = False
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            if line.startswith("data: ") and line != "data: [DONE]":
                                try:
                                    raw_data = json.loads(line[6:])
                                    if raw_data.get("error"):
                                        print(f"[STREAM INLINE ERROR] label={model_name} raw={raw_data}")
                                        had_error = True
                                        break
                                    if raw_data.get("choices"):
                                        delta = raw_data["choices"][0].get("delta", {})
                                        if "content" in delta:
                                            custom_data = {
                                                "model": model_name,
                                                "chunk": delta["content"],
                                            }
                                            yield f"data: {json.dumps(custom_data, ensure_ascii=False)}\n\n"
                                except json.JSONDecodeError:
                                    print(f"[STREAM JSON ERROR] label={model_name} line={line[:200]}")
                                    had_error = True
                                    break
                            else:
                                stripped = line.strip()
                                if stripped.startswith("{"):
                                    try:
                                        raw_data = json.loads(stripped)
                                        if raw_data.get("error"):
                                            print(f"[STREAM INLINE ERROR] label={model_name} raw={raw_data}")
                                            had_error = True
                                            break
                                    except json.JSONDecodeError:
                                        pass

                        if had_error:
                            yield error_event
                        return
        except httpx.HTTPError as e:
            print(f"[STREAM HTTP EXCEPTION] label={model_name} error={str(e)}")
            yield error_event
        except Exception as e:
            print(f"[STREAM EXCEPTION] label={model_name} error={str(e)}")
            yield error_event

    return StreamingResponse(generate(), media_type="text/event-stream")

@app.get("/models/info")
async def models_info():
    """프론트엔드에서 각 회사 라벨이 실제 그 회사 모델인지 보여주기 위한 메타데이터."""
    return {
        "mapping": MODEL_MAPPING,
        "is_real_company_model": IS_REAL_COMPANY_MODEL,
    }

@app.post("/debate/start")
async def debate_start(request: DebateRequest):
    session_id = request.session_id
    topic = request.topic

    DEBATE_SESSIONS[session_id] = {
        "topic": topic,
        "turns": [],
        "round": 1,
    }

    selected_models = random.sample(list(PERSONAS.keys()), 3)
    turns = []

    for i, model_name in enumerate(selected_models):
        previous_context = f"\n이전 발언:\n{turns[i-1]['content']}\n" if i > 0 else ""
        prompt = f"{previous_context}주제: {topic}\n\n[이전 발언 내용]을 참고하되, 당신의 페르소나에 맞춰 이 주제에 대한 당신만의 강력하고 독창적인 주장을 펼치세요."

        response = await call_ai_model(model_name, prompt, PERSONAS[model_name])
        turns.append({"model": model_name, "role": "독자적 주장", "content": response})
        await asyncio.sleep(0.5)

    DEBATE_SESSIONS[session_id]["turns"] = turns
    DEBATE_SESSIONS[session_id]["selected_models"] = selected_models

    return {"debateId": random.randint(1000, 9999), "selectedModels": selected_models, "turns": turns, "round": 1}

@app.post("/debate/continue")
async def debate_continue(request: DebateRequest):
    session_id = request.session_id
    if session_id not in DEBATE_SESSIONS:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다")

    session = DEBATE_SESSIONS[session_id]
    topic = session["topic"]
    turns_history = session["turns"]

    summary = "\n".join([f"{t['model']}: {t['content'][:100]}..." for t in turns_history[-3:]])

    selected_models = random.sample(list(PERSONAS.keys()), 3)
    turns = []

    for i, model_name in enumerate(selected_models):
        previous_context = f"\n이전 발언:\n{turns[i-1]['content']}\n" if i > 0 else ""
        prompt = f"【이전 토론 요약】\n{summary}\n\n{previous_context}주제: {topic}\n\n위 내용을 참고하여 당신의 페르소나에 맞춰 강력히 반박하거나 새로운 주장을 펼치세요."

        response = await call_ai_model(model_name, prompt, PERSONAS[model_name])
        turns.append({"model": model_name, "role": "독자적 주장", "content": response})
        await asyncio.sleep(0.5)

    session["round"] += 1
    session["turns"] = turns
    session["selected_models"] = selected_models

    return {"debateId": random.randint(1000, 9999), "selectedModels": selected_models, "turns": turns, "round": session["round"], "summary": summary}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/")
def serve_home():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(base_dir, "index.html"))

if __name__ == "__main__":
    import uvicorn
    # 렌더(Render)는 PORT 환경변수로 실제 사용할 포트를 지정해줍니다.
    # 고정된 8000 포트만 쓰면 렌더에서 헬스체크/라우팅이 실패할 수 있습니다.
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)