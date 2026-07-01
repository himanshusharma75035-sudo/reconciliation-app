# Engineer Onboarding — Eko Reconciliation App

Welcome. This is a **real-money reconciliation system** for Eko Bharat Ventures. Read this first, then the linked docs. The golden rule: **behavior is load-bearing — don't change matching, ingestion, tolerances, or status logic without understanding why it's the way it is.**

## What the app does
It ingests bank statements and internal dumps, then reconciles them per `(partner, recon_date)` — matching each transaction, flagging mismatches, and surfacing open items. It handles multiple products (core ledger, E-Value, BBPS, SBI Kiosk, AePS, QR, settlement bank) each with their own parser and money tolerance.

## The one-process architecture
A single **FastAPI** process is the whole app:
- Exposes the JSON API under `/api`
- Runs the matching engines in-process
- Runs the APScheduler cron jobs (Asia/Kolkata)
- In production, serves the built React SPA from `frontend/dist`

SQLite by default; `DATABASE_URL` switches to MySQL/PostgreSQL with no code change.

## Where things live
- `backend/routes/` — HTTP endpoints. `upload.py` is the ingestion pipeline; `recon.py` is recon operations + the Open Items window; then reports and per-product routers.
- `backend/core/` — the engines: `matching_engine.py`, `evalue_engine.py`, `bbps_engine.py`, `ingest_service.py`, `scheduler.py`, plus the Developer Portal's `portal_agent.py` / `builder_agent.py`.
- `backend/models/database.py` — **all models in one file**, plus hand-rolled idempotent migrations that run on every startup (there is no Alembic).
- `frontend/src/` — pages in `pages/`, shared UI in `components/`, one axios instance in `utils/api.js` that carries the token and rewrites every timestamp UTC→IST.

## The non-negotiables (read `docs/behavior-contract.md`)
The behavior contract lists **25 invariants**. The ones that bite newcomers:
- **Money is `float` with per-engine tolerances on purpose**: ₹1 core/BBPS, exact E-Value, 0.01 SBI, 0.02 AePS, 0.05 QR. Equalizing them silently reclassifies money.
- **Timezone pact**: backend stores naive UTC; the frontend converts to IST; Excel exports convert server-side. Never make timestamps tz-aware or ISO+Z.
- **Business dates are zero-padded strings compared lexically**, and `recon_date` can hold the sentinel `'auto'`.
- **Status sets are duplicated across ~7 files** and must stay in lockstep; the literal `'0'` counts as failed.
- **Reversal is classified before fee — deliberately.** Match IDs are `{PREFIX}-{YYYYMMDD}-{NNNN}` via MAX+1, serialized by a process-local lock (assumes a single worker).
- The ingest pipeline exists in **two slightly divergent copies** (`routes/upload.py` and `core/ingest_service.py`) — a fix in one usually needs mirroring in the other.

## Local setup
```bash
# backend
cd backend
pip install -r requirements.txt
cp .env.example .env          # set SECRET_KEY; DATABASE_URL is optional
uvicorn main:app --reload --port 8000   # Swagger at /docs

# frontend
cd frontend
npm install
npm run dev                   # http://localhost:3000, proxies /api → :8000
```
Tests: `pytest tests/ -q` from `backend/`. CI runs `compileall` + `pytest` + `npm run build` — that's the bar.

## Glossary
- **Partner** — the payment channel/product a transaction belongs to; `'mixed'` is a magic value for multi-partner dumps.
- **Open item** — a transaction not yet matched (unmatched / src_assigned / amount_mismatch).
- **SRC** — a retailer/source disposition applied to an open item.
- **Match ID** — the audit reference tying a bank row to its internal counterpart.
- **Recon run** — one execution of the matcher for a `(partner, recon_date)`.

## Using the Developer Portal
This page (the Developer Portal) is your live map: **Documentation** (these docs), **Database Schema**, **API Surface**, **Live Health**, **What's New** (recent commits, flagged when they touch load-bearing code), a read-only **Agent** for Q&A over the codebase and live data, a **Requests** queue, and — for trusted engineers — a write-capable **Builder** agent. Start with the Agent: ask it *"walk me through the ingestion → reconciliation flow"* and follow the citations.
