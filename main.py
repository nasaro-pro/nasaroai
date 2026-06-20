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

PERSONAS = {
    "OpenAI": "당신은 차갑고 객관적인 T성향의 팩트폭격기입니다. 감정을 배제하고 논리적, 데이터 기반으로 날카롭게 대답하세요.",
    "Anthropic": "당신은 따뜻하고 공감 능력이 뛰어난 F성향의 철학자입니다. 인간 중심적이고 감성적인 관점에서 대답하세요.",
    "Google": "당신은 트렌디하고 유쾌한 MZ세대 크리에이터입니다. 밈(Meme)이나 비유를 적극 활용해 톡톡 튀게 대답하세요.",
    "xAI": "당신은 냉소적이고 비판적인 아웃사이더입니다. 세상의 모순을 꼬집고 약간의 위트있는 비꼬기를 섞어 대답하세요.",
    "Perplexity": "당신은 깐깐한 학자입니다. 오직 검증된 사실과 출처, 역사적 근거를 바탕으로 진지하게 대답하세요.",
}

MODEL_MAPPING = {
    "OpenAI": "openrouter/free",
    "Anthropic": "openrouter/free",
    "Google": "openrouter/free",
    "xAI": "openrouter/free",
    "Perplexity": "openrouter/free",
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

async def call_ai_model(model_name: str, prompt: str, persona: str) -> str:
    payload = {
        "model": MODEL_MAPPING.get(model_name, "google/gemma-2-9b-it:free"),
        "messages": [
            {"role": "system", "content": persona},
            {"role": "user", "content": prompt}
        ],
        "stream": False
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=HEADERS, json=payload)
            if response.status_code == 200:
                data = response.json()
                return data.get("choices", [{}])[0].get("message", {}).get("content", "[응답 없음]")
            else:
                return f"[API 오류 {response.status_code}]"
    except Exception as e:
        return f"[통신 오류: {str(e)}]"

@app.post("/compare/stream")
async def stream_compare(data: CompareRequest):
    if data.model_name not in MODEL_MAPPING:
        raise HTTPException(status_code=400, detail="Invalid model_name")

    payload = {
        "model": MODEL_MAPPING[data.model_name],
        "messages": [
            {"role": "system", "content": PERSONAS[data.model_name]},
            {"role": "user", "content": data.message},
        ],
        "stream": True,
    }

    async def generate():
        model_name = data.model_name
        error_event = (
            f"data: {json.dumps({'model': model_name, 'error': 'API 한도 초과 또는 오류가 발생했습니다.'}, ensure_ascii=False)}\n\n"
        )

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream(
                    "POST",
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=HEADERS,
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        yield error_event
                        return

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data: ") and line != "data: [DONE]":
                            try:
                                raw_data = json.loads(line[6:])
                                if raw_data.get("error"):
                                    yield error_event
                                    return
                                if raw_data.get("choices"):
                                    delta = raw_data["choices"][0].get("delta", {})
                                    if "content" in delta:
                                        custom_data = {
                                            "model": model_name,
                                            "chunk": delta["content"],
                                        }
                                        yield f"data: {json.dumps(custom_data, ensure_ascii=False)}\n\n"
                            except json.JSONDecodeError:
                                yield error_event
                                return
                        else:
                            stripped = line.strip()
                            if stripped.startswith("{"):
                                try:
                                    raw_data = json.loads(stripped)
                                    if raw_data.get("error"):
                                        yield error_event
                                        return
                                except json.JSONDecodeError:
                                    pass
        except httpx.HTTPError:
            yield error_event
        except Exception:
            yield error_event

    return StreamingResponse(generate(), media_type="text/event-stream")

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
@app.get("/")
def serve_home():
    return FileResponse("index.html")