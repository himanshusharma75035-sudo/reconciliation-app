import os, sys, logging

# Ensure local imports resolve
sys.path.insert(0, os.path.dirname(__file__))

# Load .env FIRST — models read DATABASE_URL on import
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

from models.database import init_db, SessionLocal, Transaction
from core.auth import seed_admin

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("eko_recon")

# ── Rate Limiting (optional — install slowapi to enable) ─────────────────────
# pip install slowapi    (added to requirements.txt)
# Falls back gracefully if not installed — no disruption to existing deployments.
_slowapi_available = False
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware
    _slowapi_available = True
    logger.info("slowapi loaded — rate limiting enabled")
except ImportError:
    logger.warning(
        "slowapi not installed — rate limiting disabled. "
        "Run: pip install slowapi    to enable it."
    )
from routes.auth import router as auth_router
from routes.upload import router as upload_router
from routes.recon import router as recon_router
from routes.reports import router as reports_router
from routes.report_library import router as report_library_router
from routes.audit import router as audit_router
from routes.auto_upload import router as auto_upload_router
from routes.admin import router as admin_router
from routes.aeps_settlement import router as aeps_router
from routes.qr_settlement     import router as qr_router
from routes.settlement_bank   import router as settlement_bank_router
from routes.sbi_kiosk         import router as sbi_router
from routes.report_subscriptions import router as report_subscriptions_router
from routes.evalue import router as evalue_router
from routes.insights import router as insights_router
from routes.recon_jobs import router as recon_jobs_router
from routes.bbps import router as bbps_router
from routes.workflow import router as workflow_router
from routes.ingestion import router as ingestion_router
from routes.views import router as views_router
from routes.portal import router as portal_router

app = FastAPI(
    title="Eko Bharat Ventures — Reconciliation API",
    version="2.0.0",
    description=(
        "Full-stack reconciliation platform for Eko Bharat Ventures. "
        "Authenticate via Bearer token (POST /api/auth/login) "
        "or X-API-Key header (admin → POST /api/auth/api-keys)."
    ),
    contact={"name": "Finance Operations", "email": "finance@eko.co.in"},
    license_info={"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
)

# Wire rate-limiting middleware only when slowapi is available
if _slowapi_available:
    _rate_limit = os.getenv("RATE_LIMIT", "120/minute")
    limiter = Limiter(key_func=get_remote_address, default_limits=[_rate_limit])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

# SECURITY: default CORS to known local dev origins, never '*'.
# Set ALLOWED_ORIGINS (comma-separated) for other deployments.
_default_origins = "http://localhost:3000,http://localhost:5173,http://127.0.0.1:3000,http://127.0.0.1:5173"
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("ALLOWED_ORIGINS", _default_origins).split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "X-API-Key"],
    expose_headers=["X-RateLimit-Limit", "X-RateLimit-Remaining"],
)

app.include_router(auth_router)
app.include_router(upload_router)
app.include_router(recon_router)
app.include_router(reports_router)
app.include_router(report_library_router)
app.include_router(audit_router)
app.include_router(auto_upload_router)
app.include_router(admin_router)
app.include_router(aeps_router)
app.include_router(qr_router)
app.include_router(settlement_bank_router)
app.include_router(sbi_router)
app.include_router(report_subscriptions_router)
app.include_router(evalue_router)
app.include_router(insights_router)
app.include_router(recon_jobs_router)
app.include_router(bbps_router)
app.include_router(workflow_router)
app.include_router(ingestion_router)
app.include_router(views_router)
app.include_router(portal_router)


def _apply_indexes():
    """
    Apply indexes to existing tables (CREATE INDEX IF NOT EXISTS — safe to repeat).
    Needed because SQLAlchemy's create_all() skips tables that already exist,
    so indexes added to the model after initial creation must be applied manually.
    """
    from sqlalchemy import text
    from models.database import engine

    # Add match_id column if it doesn't exist yet (safe on both SQLite and MySQL)
    from models.database import DATABASE_URL
    add_col_sql = (
        "ALTER TABLE transactions ADD COLUMN match_id VARCHAR(40)"
        if "mysql" in DATABASE_URL
        else "ALTER TABLE transactions ADD COLUMN match_id TEXT"
    )
    with engine.connect() as conn:
        try:
            conn.execute(text(add_col_sql))
            conn.commit()
        except Exception:
            pass  # column already exists

    indexes = [
        # Composite: Dashboard queries — give me all rows for fino / 2026-04-15 / bank
        "CREATE INDEX IF NOT EXISTS ix_txn_partner_date_side ON transactions (partner, recon_date, side)",
        # Composite: Open Items queries — give me all unmatched rows for fino / 2026-04-15
        "CREATE INDEX IF NOT EXISTS ix_txn_partner_date_status ON transactions (partner, recon_date, recon_status)",
        # Composite: Matching engine inner loop — give me all unmatched txn rows
        "CREATE INDEX IF NOT EXISTS ix_txn_status_type ON transactions (recon_status, row_type)",
        # Single: match pair resolution
        "CREATE INDEX IF NOT EXISTS ix_txn_matched_with ON transactions (matched_with_id)",
        # Single: recon match ID lookup (shared across matched pair)
        "CREATE INDEX IF NOT EXISTS ix_txn_match_id ON transactions (match_id)",
        # Composite: TID lookups scoped by partner (Open Items batch TID search, cross-ref)
        "CREATE INDEX IF NOT EXISTS ix_txn_eko_tid_partner ON transactions (eko_tid, partner)",
        # E-Value query hot paths
        "CREATE INDEX IF NOT EXISTS ix_evbank_acct_status ON evalue_bank_txns (reco_acc_no, recon_status)",
        "CREATE INDEX IF NOT EXISTS ix_evload_acct_status ON evalue_wallet_loads (reco_acc_no, recon_status)",
    ]

    with engine.connect() as conn:
        for sql in indexes:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # index already exists or DB doesn't support IF NOT EXISTS — safe to skip


def _repair_duplicate_match_ids():
    """
    Detect match_ids that are shared by more than 2 transactions — a symptom of the old
    backfill bug where _next_seq() always returned 1 before anything was committed.
    Resets (NULLs) those match_ids so _backfill_match_ids can re-assign correctly.
    Safe to run every startup — is a no-op when nothing is wrong.
    """
    from sqlalchemy import func
    from models.database import engine

    db = SessionLocal()
    try:
        bad = (
            db.query(Transaction.match_id)
            .filter(Transaction.match_id.isnot(None))
            .group_by(Transaction.match_id)
            .having(func.count(Transaction.id) > 2)
            .all()
        )
        if not bad:
            return

        bad_vals = [r[0] for r in bad]
        db.query(Transaction).filter(Transaction.match_id.in_(bad_vals)).update(
            {"match_id": None}, synchronize_session=False
        )
        db.commit()
        print(f"[startup] Repaired {len(bad_vals)} duplicate match_id group(s) — will re-assign on backfill")
    except Exception as e:
        print(f"[startup] match_id repair error: {e}")
        db.rollback()
    finally:
        db.close()


def _backfill_match_ids():
    """
    Assign sequential match IDs to matched pairs that have no match_id yet.
    Uses per-series in-memory counters so IDs stay sequential within one run
    without needing intermediate commits (fixes the 'all pairs get -0001' bug).
    Safe to run every startup.
    """
    from core.matching_engine import _make_match_id, _next_seq

    db = SessionLocal()
    try:
        untagged = db.query(Transaction).filter(
            Transaction.recon_status.in_(["matched", "manual_matched", "amount_mismatch"]),
            Transaction.matched_with_id.isnot(None),
            Transaction.match_id.is_(None)
        ).all()

        if not untagged:
            return

        id_map = {t.id: t for t in untagged}

        # ── Key fix: per-series counter tracked in memory, not re-queried per row ──
        # _next_seq queries the DB for the *starting* value of the series, then we
        # increment in memory. Without this, every call returns 1 (nothing committed yet).
        seq_counters: dict = {}   # (partner, date) → current seq
        processed: set = set()

        # Sort by (partner, date) so the series stay contiguous
        for txn in sorted(untagged, key=lambda t: (t.partner or "", t.recon_date or "")):
            if txn.id in processed:
                continue

            partner = txn.partner or "manual"
            date    = txn.recon_date or ""
            key     = (partner, date)

            if key not in seq_counters:
                # Ask DB once for the starting point of this series
                seq_counters[key] = _next_seq(partner, date, db)
            else:
                seq_counters[key] += 1

            mid = _make_match_id(partner, date, seq_counters[key])
            txn.match_id = mid
            processed.add(txn.id)

            counterpart = id_map.get(txn.matched_with_id)
            if counterpart and counterpart.id not in processed:
                counterpart.match_id = mid
                processed.add(counterpart.id)

        db.commit()
        print(f"[startup] Backfilled match_id for {len(processed)} transactions ({len(processed)//2} pairs)")
    except Exception as e:
        print(f"[startup] match_id backfill error: {e}")
        db.rollback()
    finally:
        db.close()


def _backfill_bank_descriptions():
    """
    Populate Transaction.bank_description for pre-existing BANK-SIDE rows created
    before this column existed (it is filled on ingest going forward). The
    original narration was always preserved inside raw_data; this re-derives it
    using the same precedence as ingestion: the session's mapped description
    column, then common bank-statement header names, then an IMPS/NEFT/RTGS scan.

    Internal-dump rows are never touched (they must stay NULL → never displayed).
    Idempotent + batched: only rows where bank_description IS NULL are processed,
    and every processed row is set to a non-NULL value ("" when there is no
    narration), so it drains to a no-op after the first run.
    """
    import json as _json, re as _re
    from models.database import UploadSession

    _DESC_HEADERS = {
        "description", "particulars", "particular", "narration", "narrative",
        "remarks", "remark", "details", "transaction details", "transaction remarks",
    }

    db = SessionLocal()
    try:
        _map_cache: dict = {}   # upload_session_id -> description file-column (or None)

        def _desc_col_for(session_id):
            if session_id in _map_cache:
                return _map_cache[session_id]
            col = None
            if session_id:
                sess = db.query(UploadSession).filter(UploadSession.id == session_id).first()
                raw_map = sess.column_mapping if sess else None
                if raw_map:
                    try:
                        m = raw_map if isinstance(raw_map, dict) else _json.loads(raw_map)
                        col = (m.get("description") or None) if isinstance(m, dict) else None
                    except Exception:
                        col = None
            _map_cache[session_id] = col
            return col

        total = 0
        while True:
            batch = (db.query(Transaction)
                       .filter(Transaction.side == "bank",
                               Transaction.bank_description.is_(None))
                       .limit(1000).all())
            if not batch:
                break
            for t in batch:
                desc = ""
                try:
                    row = _json.loads(t.raw_data) if t.raw_data else {}
                except Exception:
                    row = {}
                if isinstance(row, dict) and row:
                    dc = _desc_col_for(t.upload_session_id)
                    if dc and dc in row:
                        desc = str(row.get(dc, "")).strip()
                    if not desc:
                        for k, v in row.items():
                            if str(k).strip().lower() in _DESC_HEADERS:
                                desc = str(v).strip()
                                if desc:
                                    break
                    if not desc:
                        for v in row.values():
                            if _re.search(r'(?:IMPS|NEFT|RTGS)/', str(v), _re.IGNORECASE):
                                desc = str(v).strip()
                                break
                t.bank_description = desc   # "" marks the row processed (drops out of IS NULL)
            db.commit()
            total += len(batch)
        if total:
            print(f"[startup] Backfilled bank_description for {total} bank transaction(s)")
    except Exception as e:
        print(f"[startup] bank_description backfill error: {e}")
        db.rollback()
    finally:
        db.close()


def _backfill_csp_fields():
    """
    Populate Transaction.csp_code / csp_name for pre-existing INTERNAL-SIDE rows
    created before these columns existed (they are filled on ingest going
    forward). The original dump row was always preserved inside raw_data; this
    re-derives the CSP identity from it using the EXACT same column lookup as
    ingestion (_extract_csp → CSPCode / MerchantName, alias-tolerant).

    Bank-side rows are never touched (they must stay NULL → never displayed).
    Idempotent + batched: only rows where csp_code IS NULL are processed, and
    every processed row is set to a non-NULL value ("" when the dump carried no
    CSP column), so it drains to a no-op after the first run.
    """
    import json as _json
    from routes.upload import _extract_csp

    db = SessionLocal()
    try:
        total = 0
        while True:
            batch = (db.query(Transaction)
                       .filter(Transaction.side == "internal",
                               Transaction.csp_code.is_(None))
                       .limit(1000).all())
            if not batch:
                break
            for t in batch:
                try:
                    row = _json.loads(t.raw_data) if t.raw_data else {}
                except Exception:
                    row = {}
                code, name = _extract_csp(row) if isinstance(row, dict) and row else ("", "")
                t.csp_code = code   # "" marks the row processed (drops out of IS NULL)
                t.csp_name = name
            db.commit()
            total += len(batch)
        if total:
            print(f"[startup] Backfilled CSP code/name for {total} internal transaction(s)")
    except Exception as e:
        print(f"[startup] csp backfill error: {e}")
        db.rollback()
    finally:
        db.close()


@app.on_event("startup")
def startup():
    """
    Runs once when the server starts.
    - init_db()                    → CREATE TABLE IF NOT EXISTS for all models (safe to repeat)
    - _apply_indexes()             → CREATE INDEX IF NOT EXISTS (safe to repeat)
    - _repair_duplicate_match_ids()→ NULL out match_ids shared by >2 rows (old backfill bug fix)
    - _backfill_match_ids()        → assign correct sequential match IDs to untagged pairs
    - seed_admin()                 → creates default admin user if no users exist
    - seed_watch_folder_configs()  → create the 4 default auto-upload configs if missing
    - start_scheduler()            → start APScheduler; reload saved cron jobs from DB
    """
    init_db()
    # Additive config/entitlement change auditing (roadmap 1.1): attach the
    # before_flush listener so admin/auth config changes by a logged-in user are
    # recorded as AuditLog rows. Actor-less sessions (seeders/scheduler) are skipped.
    try:
        from core.config_audit import register as _register_config_audit
        _register_config_audit(SessionLocal)
    except Exception as _e:
        print(f"[startup] config_audit register skipped: {_e}")
    # Index creation and one-off data repairs must NOT be able to crash startup —
    # a single hiccup here would otherwise take the whole API down. Each is
    # independent and safe to skip for this boot.
    for _step in (_apply_indexes, _repair_duplicate_match_ids, _backfill_match_ids,
                  _backfill_bank_descriptions, _backfill_csp_fields):
        try:
            _step()
        except Exception as _e:
            print(f"[startup] {_step.__name__} skipped: {_e}")
    # ── Seed reference data (non-fatal) ──────────────────────────────────────
    db = SessionLocal()
    try:
        seed_admin(db)
        # roadmap 1.2: one-time grandfather of existing users' audit_read perm
        from models.database import seed_audit_read_grandfather
        seed_audit_read_grandfather(db)
        from models.database import (
            seed_watch_folder_configs, seed_partner_configs,
            seed_bank_format_presets, seed_match_rules
        )
        seed_watch_folder_configs(db)
        seed_partner_configs(db)
        seed_bank_format_presets(db)
        seed_match_rules(db)
        from models.database import seed_evalue_accounts, seed_bank_accounts
        seed_evalue_accounts(db)
        seed_bank_accounts(db)
    except Exception as e:
        print(f"[startup] Seed warning: {e}")
        db.rollback()
    finally:
        db.close()

    # ── Scheduler (non-fatal — app works without it) ───────────────────────
    db2 = SessionLocal()
    try:
        from core.scheduler import start_scheduler, reload_all_schedules, register_eod_email_job
        start_scheduler()
        reload_all_schedules(db2)
        # Re-register all active personal report subscriptions (self-service EOD/weekly/monthly)
        from core.report_scheduler import reload_all_report_subscriptions
        reload_all_report_subscriptions(db2)
        # Re-register Developer Portal scheduled-agent jobs (Phase 4 — additive)
        try:
            from core.portal_scheduler import reload_all_portal_jobs
            reload_all_portal_jobs(db2)
        except Exception as _pe:
            print(f"[startup] Portal scheduler warning: {_pe}")
        # Optional E-Value watch-folder auto-pickup (set EVALUE_WATCH_FOLDER in .env)
        _ev_folder = os.getenv("EVALUE_WATCH_FOLDER")
        if _ev_folder:
            from core.scheduler import get_scheduler
            from apscheduler.triggers.cron import CronTrigger
            _ev_hr = int(os.getenv("EVALUE_WATCH_HOUR", "7"))
            _ev_mn = int(os.getenv("EVALUE_WATCH_MINUTE", "30"))
            def _ev_watch_job():
                from models.database import SessionLocal
                from routes.evalue import evalue_auto_pickup
                _wdb = SessionLocal()
                try:
                    evalue_auto_pickup(_ev_folder, _wdb, "auto-upload", True)
                except Exception as _e:
                    print(f"[startup] E-Value watch error: {_e}")
                finally:
                    _wdb.close()
            get_scheduler().add_job(_ev_watch_job,
                CronTrigger(hour=_ev_hr, minute=_ev_mn, timezone="Asia/Kolkata"),
                id="evalue_watch", replace_existing=True, misfire_grace_time=3600)
            print(f"[startup] E-Value watch folder registered: {_ev_folder} @ {_ev_hr:02d}:{_ev_mn:02d} IST")
    except Exception as e:
        print(f"[startup] Scheduler warning (auto-upload cron disabled): {e}")
    finally:
        db2.close()


@app.on_event("shutdown")
def shutdown():
    from core.scheduler import shutdown_scheduler
    shutdown_scheduler()


@app.get("/api/health", tags=["system"])
def health():
    """
    Health check endpoint — used by load balancers, monitoring, and the OpenClaw agent
    to verify the API is reachable and the DB is connected before executing requests.
    Returns 200 if healthy, 503 if the DB is unreachable.
    """
    from models.database import DATABASE_URL, engine
    from sqlalchemy import text
    db_type = "mysql" if "mysql" in DATABASE_URL else "sqlite"
    db_ok = True
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        db_ok = False
        logger.error(f"Health check DB failure: {e}")

    import time
    payload = {
        "status":    "ok" if db_ok else "degraded",
        "app":       "Eko Bharat Ventures — Reconciliation API",
        "version":   "2.0.0",
        "db":        db_type,
        "db_ok":     db_ok,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if not db_ok:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/api/openapi-info", tags=["system"], include_in_schema=False)
def openapi_info():
    """
    Returns key information for agents connecting to this API:
    authentication methods, rate limits, and links to full OpenAPI schema.
    """
    return {
        "api_version": "2.0.0",
        "openapi_schema": "/openapi.json",
        "swagger_ui":     "/docs",
        "redoc":          "/redoc",
        "authentication": {
            "jwt": {
                "method":   "Bearer token in Authorization header",
                "obtain":   "POST /api/auth/login with username+password",
                "expiry":   "8 hours",
            },
            "api_key": {
                "method":   "X-API-Key header",
                "obtain":   "POST /api/auth/api-keys (admin only)",
                "expiry":   "Configurable or never",
            },
        },
        "rate_limit": "120 requests/minute per IP (configurable via RATE_LIMIT env var)",
        "pagination":  "Most list endpoints support ?page=1&page_size=50",
        "health_check": "/api/health",
    }


# Serve built React app in production
frontend_dist = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
if os.path.exists(frontend_dist):
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="static")
