"""
core/scheduler.py

APScheduler singleton for auto-upload cron jobs.
One job per WatchFolderConfig that has is_enabled=True in its UploadSchedule.
Jobs survive server restarts because they are re-seeded from DB on startup.
"""

import datetime
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

_scheduler = BackgroundScheduler(timezone="Asia/Kolkata")


def get_scheduler() -> BackgroundScheduler:
    return _scheduler


def start_scheduler():
    if not _scheduler.running:
        _scheduler.start()
        logger.info("[scheduler] Started")


def shutdown_scheduler():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[scheduler] Stopped")


# ─── Job function ──────────────────────────────────────────────────────────────

def _run_auto_upload_job(config_id: str):
    """
    Called by APScheduler on the configured cron schedule.
    Each invocation opens its own DB session.
    """
    from models.database import SessionLocal, WatchFolderConfig, UploadSchedule
    from core.ingest_service import detect_file_for_today, ingest_dataframe, build_expected_filename
    from models.database import UploadSession, UploadStatus, generate_id
    from core.pdf_converter import load_file_to_dataframe

    db = SessionLocal()
    run_at = datetime.datetime.utcnow()

    try:
        config = db.query(WatchFolderConfig).filter(WatchFolderConfig.id == config_id).first()
        if not config or not config.is_enabled:
            return

        file_path, expected_name = detect_file_for_today(config)

        # ── Update config trigger status regardless of outcome ────────────────
        config.last_triggered_at    = run_at
        config.last_trigger_filename = expected_name

        if file_path is None:
            config.last_trigger_status  = "not_found"
            config.last_trigger_message = f"File not found: {expected_name}"
            db.commit()
            _update_schedule_run(db, config_id, run_at, "not_found", f"File not found: {expected_name}")
            return

        # ── Load + ingest ─────────────────────────────────────────────────────
        df = load_file_to_dataframe(file_path, expected_name)

        # Auto-detect mapping via presets
        from routes.upload import _detect_bank_format, BANK_FORMAT_PRESETS
        columns    = list(df.columns)
        preset     = _detect_bank_format(config.partner, config.side, columns)
        mapping    = preset["mapping"] if preset else _smart_default_mapping(columns)
        preset_lbl = preset["label"] if preset else "auto-detected"

        # Resolve actual column names (case-insensitive).
        # Values must be STRIPPED — ingest_dataframe() strips df.columns, so
        # row_dict keys are stripped. Without this, space-padded Excel headers
        # (e.g. Simplibank dump) produce empty fields for every row.
        col_map    = {c.strip().lower(): c.strip() for c in columns}
        mapping_resolved = {}
        for std_field, file_col in mapping.items():
            matched = col_map.get(file_col.strip().lower())
            if matched:
                mapping_resolved[std_field] = matched

        session = UploadSession(
            id=generate_id(),
            user_id=None,
            partner=config.partner,
            side=config.side,
            original_filename=expected_name,
            stored_filename=expected_name,
            recon_date="auto" if config.recon_date_mode == "auto" else datetime.date.today().strftime("%Y-%m-%d"),
            status=UploadStatus.processing,
            row_count=0,
            column_mapping=None,
        )
        db.add(session)
        db.commit()

        result = ingest_dataframe(
            df=df,
            session=session,
            mapping_dict=mapping_resolved,
            db=db,
            user_id="system",
            username="auto-upload",
            do_auto_recon=config.auto_recon,
        )

        msg = f"Ingested {result['row_count']} txn rows from {expected_name} (format: {preset_lbl})"
        config.last_trigger_status  = "success"
        config.last_trigger_message = msg
        db.commit()
        _update_schedule_run(db, config_id, run_at, "success", msg)
        logger.info(f"[scheduler] {msg}")

    except Exception as e:
        err = str(e)
        logger.error(f"[scheduler] Auto-upload job failed for config {config_id}: {err}")
        try:
            config = db.query(WatchFolderConfig).filter(WatchFolderConfig.id == config_id).first()
            if config:
                config.last_trigger_status  = "error"
                config.last_trigger_message = err
                config.last_triggered_at    = run_at
            db.commit()
            _update_schedule_run(db, config_id, run_at, "error", err)
        except Exception:
            db.rollback()
    finally:
        db.close()


def _update_schedule_run(db, config_id, run_at, status, message):
    from models.database import UploadSchedule
    try:
        sched = db.query(UploadSchedule).filter(UploadSchedule.watch_folder_id == config_id).first()
        if sched:
            sched.last_run_at      = run_at
            sched.last_run_status  = status
            sched.last_run_message = message
            db.commit()
    except Exception:
        db.rollback()


def _smart_default_mapping(columns: list) -> dict:
    """Fallback mapping when no preset matches — common column name heuristics."""
    col_lower = {c.strip().lower(): c for c in columns}
    guesses = {
        'eko_tid':          ['eko_trxn_id', 'tid', 'ekotid', 'eko_tid'],
        'tracking_number':  ['trackingnumber', 'tracking_number', 'tracking'],
        'utr_number':       ['utrnumber', 'utr_number', 'utr'],
        'amount':           ['amount', 'transferamt', 'transfer_amt'],
        'status':           ['status'],
        'transaction_date': ['transaction_date', 'txn_date', 'date', 'transaction effective date'],
        'description':      ['description', 'narration', 'remarks', 'desc'],
    }
    mapping = {}
    for std, candidates in guesses.items():
        for c in candidates:
            if c in col_lower:
                mapping[std] = col_lower[c]
                break
    return mapping


# ─── Schedule management ───────────────────────────────────────────────────────

def _job_id(config_id: str) -> str:
    return f"auto_upload_{config_id}"


def register_job(config_id: str, hour: int, minute: int):
    """Add or replace a cron job for the given config."""
    jid = _job_id(config_id)
    if _scheduler.get_job(jid):
        _scheduler.remove_job(jid)
    _scheduler.add_job(
        _run_auto_upload_job,
        trigger=CronTrigger(hour=hour, minute=minute),
        id=jid,
        args=[config_id],
        replace_existing=True,
        misfire_grace_time=600,   # allow up to 10 min late start
    )
    logger.info(f"[scheduler] Registered job {jid} at {hour:02d}:{minute:02d}")


def unregister_job(config_id: str):
    """Remove the cron job for the given config if it exists."""
    jid = _job_id(config_id)
    if _scheduler.get_job(jid):
        _scheduler.remove_job(jid)
        logger.info(f"[scheduler] Removed job {jid}")


def reload_all_schedules(db):
    """
    Called on startup. Loads all enabled UploadSchedule rows from DB
    and registers their cron jobs with APScheduler.
    """
    from models.database import UploadSchedule, WatchFolderConfig

    # Clear all existing auto-upload jobs
    for job in _scheduler.get_jobs():
        if job.id.startswith("auto_upload_"):
            job.remove()

    schedules = db.query(UploadSchedule).filter(UploadSchedule.is_enabled == True).all()
    for sched in schedules:
        config = db.query(WatchFolderConfig).filter(
            WatchFolderConfig.id == sched.watch_folder_id,
            WatchFolderConfig.is_enabled == True,
        ).first()
        if config:
            register_job(sched.watch_folder_id, sched.hour, sched.minute)

    logger.info(f"[scheduler] Loaded {len(schedules)} schedule(s) from DB")


# ─── EOD Email Summary Job ─────────────────────────────────────────────────────

def _run_eod_email_job():
    """Send daily EOD reconciliation summary email. Called by APScheduler."""
    from models.database import SessionLocal
    from core.notifications import send_eod_summary
    db = SessionLocal()
    try:
        send_eod_summary(db)
    except Exception as e:
        logger.error(f"[scheduler] EOD email job failed: {e}")
    finally:
        db.close()


def register_eod_email_job():
    """Register (or re-register) the daily EOD email job from env config."""
    import os
    hour   = int(os.getenv("EOD_EMAIL_HOUR",   "20"))
    minute = int(os.getenv("EOD_EMAIL_MINUTE", "0"))
    jid    = "eod_email_summary"

    if _scheduler.get_job(jid):
        _scheduler.remove_job(jid)

    _scheduler.add_job(
        _run_eod_email_job,
        trigger=CronTrigger(hour=hour, minute=minute, timezone="Asia/Kolkata"),
        id=jid,
        replace_existing=True,
        misfire_grace_time=1800,  # allow up to 30 min late
    )
    logger.info(f"[scheduler] EOD email job registered at {hour:02d}:{minute:02d} IST")


def _run_recycle_purge_job():
    """Purge recycle-bin batches older than the retention window. Until this job existed,
    purge_expired was never invoked anywhere — the 30-day retention the UI advertises
    was not actually enforced and the bin grew forever."""
    from models.database import SessionLocal
    from core import recycle_bin
    db = SessionLocal()
    try:
        n = recycle_bin.purge_expired(db)
        if n:
            logger.info(f"[scheduler] recycle-bin purge removed {n} expired row(s)")
    except Exception as e:
        logger.warning(f"[scheduler] recycle-bin purge failed: {e}")
    finally:
        db.close()


def register_recycle_purge_job():
    """Nightly 02:30 IST recycle-bin retention purge."""
    jid = "recycle_bin_purge"
    if _scheduler.get_job(jid):
        _scheduler.remove_job(jid)
    _scheduler.add_job(
        _run_recycle_purge_job,
        trigger=CronTrigger(hour=2, minute=30, timezone="Asia/Kolkata"),
        id=jid,
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("[scheduler] recycle-bin purge job registered at 02:30 IST")
