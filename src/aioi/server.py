"""FastAPI app — a minimal webhook-style endpoint mirroring how a Zalo OA webhook
would feed messages into the engine. Run: uvicorn aioi.server:app --reload
"""
from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from .config import load_settings
from .engine import Engine
from .llm import build_provider

app = FastAPI(title="AI Ơi Nhà Đất", version="0.1.0")

_settings = load_settings()
_engine = Engine(build_provider(_settings))


class IncomingMessage(BaseModel):
    user_key: str        # Zalo user id (sender)
    text: str            # message text (voice/image OCR would be transcribed upstream)


class Reply(BaseModel):
    assistant_name: str
    reply: str
    captured: list[str]
    next_field: str | None
    property_score: float
    user_score: float
    tier: str
    completeness: float


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "provider": _settings.effective_provider}


@app.post("/message", response_model=Reply)
def message(msg: IncomingMessage) -> Reply:
    r = _engine.handle(msg.user_key, msg.text)
    return Reply(
        assistant_name=r.assistant_name,
        reply=r.reply,
        captured=r.captured,
        next_field=r.next_field,
        property_score=r.property_score,
        user_score=r.user_score,
        tier=r.tier,
        completeness=r.completeness,
    )
