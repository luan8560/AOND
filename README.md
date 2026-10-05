# AI Ơi Nhà Đất — Conversation Engine (prototype)

Conversational real-estate assistant for **aioinhadat.com**. Sellers chat naturally on
Zalo (text / voice / images) instead of filling forms; a family of `•ai` assistants
silently extracts listing data, scores users via gamification, and asks for one missing
detail at a time.

This repo is the **first milestone**: the core conversation engine, runnable locally,
with Claude behind a swappable provider abstraction.

## What's implemented

| Module | SRD section | Purpose |
|---|---|---|
| `aioi/assistants.py` | §II | The `•ai` assistant name family + stable persona assignment (T•ai seller / M•ai buyer) |
| `aioi/schema.py` | §III | Dynamic property schema — 3 type groups + shared rental terms, gradual fill, completeness |
| `aioi/gamification.py` | §IV | Property score (50% completeness + 50% timeliness), user score × scale bonus, Đồng/Bạc/Vàng tiers |
| `aioi/prompts.py` | §II | System prompt enforcing 30-word cap, praise-first, 1-question, image-honesty; extraction schema |
| `aioi/llm/` | §VII | "Cắm Rút" provider abstraction — Claude (default), Gemini (stub), Mock (offline) |
| `aioi/engine.py` | — | Per-turn orchestration: extract → score → pick next field → reply |
| `aioi/server.py` | §VII | FastAPI webhook endpoint (Zalo-style ingress) |
| `cli.py` | — | Local REPL demo |

LLM models: **Claude Haiku 4.5** for fast ≤30-word chat, **Claude Opus 4.8** for
vision/OCR-grade extraction. The abstraction lets Gemini or a local Blackwell model
drop in later without touching the engine (SRD §VII).

## Quick start

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# Offline demo — no API key needed (uses the mock provider):
python cli.py

# Real Claude replies:
cp .env.example .env   # then set ANTHROPIC_API_KEY
python cli.py

# Run the engine as a webhook service:
uvicorn aioi.server:app --reload
```

## Tests

```bash
pytest
```

Tests run fully offline against the mock provider.

## Not yet built (next milestones)

- Zalo OA webhook integration + Keep-Alive scheduler (§VI)
- SharePoint 5-layer storage (§VII) — engine state is currently in-memory
- Image/voice OCR pipeline (Opus 4.8 vision) wired into ingress
- Bot-to-bot buyer↔seller handoff (T•ai → M•ai, §VII)
- Live Chat Monitor labels + human Takeover (§VII)
- Revenue/user classification (CCRB 1% / NMG 0.5%, §V)
