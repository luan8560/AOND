# AI Ơi Nhà Đất (AOND)

Conversational real-estate intake for **aioinhadat.com**. Sellers list property by **chatting
naturally** — Zalo-style text, voice, or photos — instead of filling forms. A family of `•ai`
assistants reads each message, quietly extracts the listing, reasons over shared project
knowledge, and asks for one missing detail at a time.

> Trợ lý BĐS hội thoại: người rao **nhắn tự nhiên**, AOND tự bóc tách & dựng tin rao — không form.

Built on **FastAPI + Claude** behind a swappable provider abstraction, with **SQLite + SharePoint**
storage. Runs locally today (CLI + webhook); a real Zalo channel is the next milestone.

---

## Three data principles (the heart of AOND)

1. **CHUNG vs RIÊNG** — *shared vs private.* "What a căn **is**" (catalog: models, areas, site map,
   handover spec, zoning, developer) is **shared** knowledge, reused across every seller. "How this
   seller **offers** it" (price, which units they advertise) is **private** to that seller. Sending a
   price list ≠ advertising — a file is stored as reference; the basket fills only on an explicit *rao* intent.
2. **Never delete data** — a seller can only **rao** (advertise), **không rao** (unlist — hidden but
   kept), or **thôi đăng ký** (withdraw). There is no user-facing delete and no one-shot wipe; the
   "remove/clear" operations flip a listing's status, never drop the row. Destructive deletion exists
   only as a test/admin tool.
3. **When unsure, ask** — AOND confirms before anything irreversible or ambiguous (a destructive op, a
   project name that looks like a mishearing or a model-name-as-project) instead of guessing.

See [`docs/data-model.md`](docs/data-model.md) for the full model.

---

## Architecture

```
Zalo / CLI ──▶ Engine ──▶ LLMProvider (Claude · Gemini-stub · Mock)
                 │
                 ├─ SQLite (aioi.db): BẢN GHI
                 │     projects (CHUNG: catalog, models, docs, inventory, votes)
                 │     conversations (RIÊNG: history, price_book)
                 │     listings (RIÊNG: rổ hàng + status + media refs)
                 │
                 └─ SharePoint (kho gốc): FILE THẬT (bytes)
                       <Dự án>/_Chung · <Mẫu nhà>/_Chung · <listing>/ · Nhà lẻ/
                       (DB stores only a MediaRef — never the bytes)
```

- **Provider abstraction** (`aioi/llm/`, the "Cắm–Rút" layer): `chat`, `extract`, `describe_media`,
  `group_media`, `converse` (tool-using agent loop). Claude is default; Mock runs fully offline.
- **Models:** chat/extraction on **Claude Haiku 4.5** (cheap, structured), vision/OCR on **Opus 4.8**.
  Set `AIOI_CHAT_MODEL=claude-sonnet-4-6` for higher-quality replies. **Prompt caching** on the system
  prompt cuts the cost of the repeated project-memory block across agent-loop steps.
- **Engine flow per turn:** pending-confirm → grounded Q&A → media/drawing request → extract to an
  ordered list of basket operations → reply (agent loop for project sellers, else a single chat call).

---

## Quick start

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1          # PowerShell  (bash: . .venv/bin/activate)
pip install -e ".[dev]"
```

```bash
# Offline demo — no API key, no cost (mock provider):
$env:AIOI_LLM_PROVIDER = "mock"; python cli.py

# Real Claude replies (needs Anthropic API credit):
copy .env.example .env               # then set ANTHROPIC_API_KEY
python cli.py

# Run as a webhook service (Zalo-style ingress):
uvicorn aioi.server:app --reload     # GET /health · POST /message
```

### CLI commands

| Command | Does |
|---|---|
| `/state` | Everything AOND extracted: every listing + fields, saved prices, shared project knowledge |
| `/listings` · `/switch <n>` · `/new` | Basket overview · pick a listing · start a new one |
| `/seller <id>` · `/whoami` | Switch seller identity (own basket & prices) · show current |
| `/link <url>` | Send a photo / PDF / price-list link — AOND auto-detects and reads it |
| `/project <mô tả>` · `/import <file.csv>` | Register a project from text · bulk-import listings |
| `/sold` · `/status <tt>` · `/quit` | Mark sold · set status · exit |

## Tests

```bash
pytest          # 231 tests, fully offline against the mock provider
```

## Configuration (`.env`)

| Key | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API (required for real replies) |
| `AIOI_LLM_PROVIDER` | `claude` (default) or `mock` (offline) |
| `AIOI_CHAT_MODEL` / `AIOI_EXTRACT_MODEL` / `AIOI_VISION_MODEL` | model overrides |
| `GOOGLE_API_KEY` | Google Drive folder/file link resolution (optional) |
| `AZURE_*` / `SHAREPOINT_SITE_ID` | SharePoint media store (optional; falls back to read-only) |

**Never committed:** `.env` and `*.db` (+ backups) are git-ignored — they hold real keys and real
seller data. See `.env.example` for the template.

## Project layout

```
src/aioi/
  engine.py        orchestration: extract → operations → reply; grounded Q&A; media ingest
  prompts.py       system prompts + extraction schemas (chat, inventory, operations, scope)
  projects.py      Project / UnitType / Unit + vote-based shared knowledge (provenance)
  inventory.py     attribute-agnostic queries over a project's units (house & condo alike)
  schema.py        Property model: type groups, gradual fill, completeness
  gamification.py  property score, user score, tiers
  sharepoint.py    media store (kho gốc) + canonical folder layout; PII redaction for sổ đỏ
  llm/             provider abstraction: base · claude · gemini(stub) · mock
  server.py        FastAPI webhook ; drive.py fetch.py imaging.py delivery.py bulk.py revenue.py
scripts/reconcile_aioi.py   admin: merge duplicate projects / tidy SharePoint folders (dry-run default)
cli.py             local REPL ; docs/data-model.md   the storage & inheritance design
```

## Roadmap

- Zalo OA webhook + Keep-Alive scheduler, voice→text ingest
- Background web-search enrichment (source-cited `.md`), buyer↔seller bot handoff
- Richer project model (towers/phases, price-over-time) — added lazily, simple case stays simple

---

*Prototype — core conversation engine, storage, and media/price-list ingest are built and tested.*
