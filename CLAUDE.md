# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read this first — behavior preservation

This app reconciles **real money**. A large set of behaviors are deliberate and load-bearing: changing any of them silently reclassifies live transactions, breaks audit references, or double-counts funds.

- **`docs/behavior-contract.md` lists 25 invariants.** Before editing anything under `backend/core/` or `backend/routes/upload.py`, read it and `docs/architecture.md`. When a change touches matching, ingestion/parsing, row classification, status transitions, tolerances, or match-ID generation, call it out explicitly — these are not refactor-at-will code.
- Deliberately-unfixed issues are catalogued in `docs/known-issues.md`. Don't "fix" something that's documented there without checking why it's intentional.
- The two divergent ingest copies (see below) and a layering inversion are known warts kept on purpose; touch them only with characterization tests in place.
- **`docs/skills.md` is the transferable playbook** — the domain model, the patterns, and the traps that have actually bitten this codebase (ID sequencing, replace-semantics, status-set drift, hidden "other" buckets, pair de-dup, deploy/asset pitfalls). Read it before designing anything new; it explains *why* the conventions below exist.

**Keep `docs/skills.md` current — same PR, not later.** When you add a capability, hit a non-obvious bug, or learn something that would have saved you a day, add the lesson there. It ships to a **public** repo: no credentials, hostnames/IPs, account numbers, customer data, or personal names — patterns only.

## Commands

All backend commands run from `backend/`; all frontend commands from `frontend/`.

**Backend (FastAPI):**
```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000     # http://localhost:8000 ; Swagger at /docs, ReDoc at /redoc
pytest tests/ -q                          # tests (pyproject sets pythonpath=["."] so bare `pytest` works)
pytest tests/test_evalue_engine.py::test_name -q   # single test
ruff check .                              # lint (config in pyproject.toml, rules E9+F; install ruff on demand — not pinned)
python -m compileall -q core models routes main.py # syntax check (this is what CI runs, plus pytest)
python migrate_to_mysql.py                # copy SQLite data into the MySQL named by DATABASE_URL
```

**Frontend (React 18 + Vite):**
```bash
npm install            # CI uses `npm ci`
npm run dev            # http://localhost:3000 ; Vite proxies /api → :8000
npm run build          # production build into frontend/dist (CI gate; backend serves this in prod)
```

**Both at once:** `./scripts/start.sh` (macOS/Linux) or `scripts\start.bat` (Windows); `scripts\start_backend.bat` for backend only (all launch scripts live in `scripts/`). **Docker:** `docker compose up --build` builds the frontend and serves the whole app from one container at `http://localhost:8000`.

CI (`.github/workflows/ci.yml`) is the bar to clear: backend `compileall` + `pytest tests/ -q`, frontend `npm run build`. Note the test suite is currently thin (E-Value engine only) — most logic is unguarded, which is exactly why the behavior contract matters.

## Architecture (the big picture)

It's a **single FastAPI process**: it exposes the JSON API under `/api`, runs the matching engines and the APScheduler (Asia/Kolkata) cron jobs in-process, and in production serves the built React SPA from `frontend/dist`. One process / one container is the whole app. SQLite by default; `DATABASE_URL` switches to MySQL/PostgreSQL with no code change.

**Layers:**
- `backend/routes/` — HTTP endpoints. `upload.py` is the ingestion pipeline (format presets, FREC/WLR wrong-file checks, the `_parse_description` regex ladder, `_classify_bank_row`). `recon.py` is recon operations + the universal Open Items window. Then `reports.py`, product routers (`evalue.py`, `bbps.py`, `sbi_kiosk.py`, `aeps_settlement.py`, `qr_settlement.py`, `settlement_bank.py`), platform routers (`auth.py`, `admin.py`, `audit.py`, `insights.py`, `workflow.py`, `auto_upload.py`, …), and the additive observability/governance routers (`ingestion.py` — ledger/monitor/sources/health, `views.py` — saved views).
- `backend/core/` — engines: `matching_engine.py` (priority-rule matcher + special passes), `evalue_engine.py` (8 bank parsers + 5-pass matcher), `bbps_engine.py`, `ingest_service.py`, `scheduler.py`, `auth.py`, `maker_checker.py`, `file_hash_guard.py`, `pdf_converter.py`. Additive governance/observability helpers (read-only, never touch matching): `config_audit.py`, `ingestion_ledger.py`, `data_quality.py`, `ingestion_sources.py`, `recon_health.py`.
- `backend/models/database.py` — **all 45 models in one file**, plus hand-rolled idempotent migrations and seed functions. There is no Alembic: on **every startup** the app runs `create_all`, idempotent `ALTER TABLE` column-adds, a match-ID repair/backfill pass, and seeders. Seeders double as data migrations and are keyed on **exact strings** (renames, flag flips) — so editing those literals re-triggers or skips migrations on deployed databases.
- `backend/instance/` — deployment-specific data (the bank-account registry), gitignored.
- `frontend/src/` — pages in `pages/`, shared UI in `components/`, shared classes in `index.css`. **One axios instance** (`utils/api.js`) carries the Bearer token, does a global 401→`/login` redirect, and runs a **global response interceptor that rewrites every datetime-shaped string from UTC to IST**. Pages contain no timezone logic.

**The ingestion → reconciliation flow** (in `docs/architecture.md` with diagrams):
1. `POST /api/upload/file` — parse (auto header detection, multi-page PDF), WLR/FREC wrong-file checks (422 hard-block), auto-detect column mapping, return preview.
2. `POST /api/upload/confirm-mapping` — 409 slot guard + SHA-256 file-hash guard against re-upload; per-row classification (**reversal is checked before fee — deliberately**); identifier extraction via an order-sensitive regex ladder; per-partner filters; mixed dumps fan out to partners.
3. **Auto-recon chain, and the order is load-bearing:** reversal match → counterpart-gated `run_reconciliation` → NEFT D+1 → internal self-match. Each step's errors are swallowed so a failure never blocks the upload.
4. Core matching is per `(partner, recon_date)`, priority rules, first-match-wins; within ±₹1 → `matched`, else `amount_mismatch`. Match IDs are `{PREFIX}-{YYYYMMDD}-{NNNN}` via **MAX+1** sequencing, serialized by a **process-local lock — this assumes a single worker** (multi-worker needs an atomic sequence first; see `docs/mysql-migration.md`).

**Two things that will surprise you:** (1) the ingest pipeline exists in **two slightly divergent copies** — the interactive path in `routes/upload.py` and the watch-folder path in `core/ingest_service.py` — and a fix in one usually needs mirroring in the other (behavior-contract item 10). (2) `core/ingest_service.py` and `core/scheduler.py` **import helpers from `routes/upload.py`** (a deliberate layering inversion).

## Conventions you'll trip over

- **Timezone pact:** backend stores naive UTC (`datetime.utcnow`); the frontend interceptor converts to IST; Excel exports convert server-side. Never make timestamps tz-aware or ISO+Z — it double-shifts every displayed time.
- **Money is `float`** (`MONEY = Numeric(15,2, asdecimal=False)`) with **per-engine tolerances on purpose**: ₹1 core/BBPS, exact E-Value, 0.01 SBI, 0.02 AePS, 0.05 QR. Equalizing them reclassifies money.
- **Business dates are zero-padded strings compared lexicographically** (not date objects). `recon_date` legitimately holds the sentinel `'auto'` / `'auto (multi-date)'`.
- **Status sets (`FAILED_STATUSES`, open-vs-matched buckets) are duplicated across ~7 files** and must stay in lockstep; the literal `'0'` counts as failed.
- **Magic literals are contracts:** partner `'mixed'`; usernames `'system'`/`'auto-upload'`/`'openclaw-bot'` drive audit-source classification; error prefixes `[WLR]`/`[FREC]` are string-matched by the frontend; Excel headers/sheet names and upload result field names are external contracts.

## Configuration

- **`backend/.env`** — copy from `.env.example`. `SECRET_KEY` is required (the app refuses to start in production without it). `DATABASE_URL` selects the DB engine. SMTP and schedule times are optional.
- **`backend/instance/seed_accounts.json`** — the bank-account registry (gitignored; real account numbers never go in the repo). Copy `seed_accounts.example.json`. `reco_acc_no` values must exactly equal the internal dump's `source` labels, including deliberate quirks (behavior-contract item 13).
- **Never commit** `.env`, databases, bank statements/dumps, or any `*.docx`/`*.xlsx`/`*.pdf` — `.gitignore` blocks them, and the repo has public remotes. Matching rules are editable per partner in the UI (Logic Builder), not in code.
