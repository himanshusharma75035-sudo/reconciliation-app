"""
routes/auto_upload.py

Endpoints for the Auto Upload feature:
  GET  /api/auto-upload/configs              — list all 4 folder configs
  PUT  /api/auto-upload/configs/{id}         — save folder path + naming settings
  POST /api/auto-upload/trigger/{id}         — one-click manual trigger
  GET  /api/auto-upload/configs/{id}/test    — test folder path (check exists + today's filename)
  GET  /api/auto-upload/schedules            — list all schedules
  PUT  /api/auto-upload/schedules/{config_id} — save/update schedule (hour:minute)
  POST /api/auto-upload/schedules/{config_id}/toggle — enable or disable a schedule
"""

import datetime
import json
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from models.database import (
    get_db, WatchFolderConfig, UploadSession, UploadStatus,
    UploadSchedule, generate_id, User
)
from core.auth import get_current_user, require_permission

router = APIRouter(prefix="/api/auto-upload", tags=["auto-upload"])


# ─── Pydantic schemas ──────────────────────────────────────────────────────────

class FolderConfigUpdate(BaseModel):
    folder_path:     Optional[str] = None
    file_prefix:     Optional[str] = None
    file_suffix:     Optional[str] = ".xlsx"
    date_format:     Optional[str] = "YYYYMMDD"
    recon_date_mode: Optional[str] = "auto"
    auto_recon:      Optional[bool] = True
    is_enabled:      Optional[bool] = True


class ScheduleUpdate(BaseModel):
    hour:       int = 8
    minute:     int = 0
    is_enabled: bool = True


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _config_to_dict(c: WatchFolderConfig, sched: UploadSchedule = None) -> dict:
    return {
        "id":                   c.id,
        "label":                c.label,
        "partner":              c.partner,
        "side":                 c.side,
        "folder_path":          c.folder_path or "",
        "file_prefix":          c.file_prefix or "",
        "file_suffix":          c.file_suffix or ".xlsx",
        "date_format":          c.date_format or "YYYYMMDD",
        "recon_date_mode":      c.recon_date_mode or "auto",
        "auto_recon":           c.auto_recon if c.auto_recon is not None else True,
        "is_enabled":           c.is_enabled if c.is_enabled is not None else True,
        "last_triggered_at":    str(c.last_triggered_at)    if c.last_triggered_at    else None,
        "last_trigger_status":  c.last_trigger_status  or None,
        "last_trigger_message": c.last_trigger_message or None,
        "last_trigger_filename":c.last_trigger_filename or None,
        "schedule": {
            "id":             sched.id          if sched else None,
            "hour":           sched.hour        if sched else 8,
            "minute":         sched.minute      if sched else 0,
            "is_enabled":     sched.is_enabled  if sched else False,
            "last_run_at":    str(sched.last_run_at)   if sched and sched.last_run_at   else None,
            "last_run_status":sched.last_run_status     if sched else None,
            "last_run_message":sched.last_run_message   if sched else None,
        } if sched else None,
    }


def _get_sched(db: Session, config_id: str) -> Optional[UploadSchedule]:
    return db.query(UploadSchedule).filter(UploadSchedule.watch_folder_id == config_id).first()


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("/configs")
def list_configs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Self-heal: ensure table exists and default rows are seeded
    # (handles first-run edge cases where startup seed was skipped)
    try:
        from models.database import Base, engine, seed_watch_folder_configs
        Base.metadata.create_all(bind=engine)
        configs = db.query(WatchFolderConfig).order_by(
            WatchFolderConfig.partner, WatchFolderConfig.side
        ).all()
        if not configs:
            seed_watch_folder_configs(db)
            configs = db.query(WatchFolderConfig).order_by(
                WatchFolderConfig.partner, WatchFolderConfig.side
            ).all()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Config load error: {e}")
    return [_config_to_dict(c, _get_sched(db, c.id)) for c in configs]


@router.put("/configs/{config_id}")
def update_config(
    config_id: str,
    body: FolderConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("upload")),
):
    config = db.query(WatchFolderConfig).filter(WatchFolderConfig.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")

    if body.folder_path is not None:
        # SECURITY: normalise and validate — must be an absolute, existing directory
        # with no traversal tricks, and not a sensitive system location.
        raw = body.folder_path.strip()
        if raw:
            resolved = os.path.realpath(raw)
            if not os.path.isabs(resolved):
                raise HTTPException(status_code=400, detail="Folder path must be absolute")
            _norm = resolved.replace("\\", "/").lower()
            _blocked = ("/etc", "/bin", "/usr", "/root", "/boot", "/proc", "/sys",
                        "c:/windows", "c:/program files", "c:/program files (x86)")
            if any(_norm == b or _norm.startswith(b + "/") for b in _blocked):
                raise HTTPException(status_code=400, detail="Folder path points to a protected system location")
            config.folder_path = resolved
        else:
            config.folder_path = ""
    if body.file_prefix is not None:
        config.file_prefix = body.file_prefix
    if body.file_suffix is not None:
        config.file_suffix = body.file_suffix
    if body.date_format is not None:
        config.date_format = body.date_format
    if body.recon_date_mode is not None:
        config.recon_date_mode = body.recon_date_mode
    if body.auto_recon is not None:
        config.auto_recon = body.auto_recon
    if body.is_enabled is not None:
        config.is_enabled = body.is_enabled

    config.updated_at    = datetime.datetime.utcnow()
    config.created_by    = current_user.id
    db.commit()
    return _config_to_dict(config, _get_sched(db, config_id))


@router.get("/configs/{config_id}/test")
def test_folder_path(
    config_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Check whether the configured folder exists and today's file is present."""
    from core.ingest_service import detect_file_for_today, build_expected_filename

    config = db.query(WatchFolderConfig).filter(WatchFolderConfig.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")

    folder_exists   = bool(config.folder_path) and os.path.isdir(config.folder_path)
    expected_name   = build_expected_filename(config)
    file_path, _    = detect_file_for_today(config)
    file_exists     = file_path is not None

    return {
        "folder_path":     config.folder_path or "",
        "folder_exists":   folder_exists,
        "expected_filename": expected_name,
        "file_exists":     file_exists,
        "file_path":       file_path,
    }


@router.post("/trigger/{config_id}")
def trigger_upload(
    config_id: str,
    force: bool = False,   # Re-upload (replace) — threaded to the module handlers' guards
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("upload")),
):
    """
    One-click manual trigger. Finds today's file in the configured folder,
    ingests it, and (if auto_recon is on) runs the matching engine.

    SBI Kiosk files (partner='sbi') are routed to the SBI-specific upload
    endpoints instead of the standard ingest_dataframe flow.
    """
    from core.ingest_service import detect_file_for_today, ingest_dataframe
    from core.pdf_converter import load_file_to_dataframe
    from routes.upload import _detect_bank_format

    config = db.query(WatchFolderConfig).filter(WatchFolderConfig.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")
    if not config.folder_path:
        raise HTTPException(status_code=400, detail="Folder path not configured")

    # ── SBI Kiosk special routing ─────────────────────────────────────────────
    # SBI files use specialised endpoints that can't go through ingest_dataframe.
    # The 'side' field encodes the SBI file type (bank_statement, ko_limits, etc.)
    if config.partner == "sbi":
        return _trigger_sbi_upload(config, current_user, db)

    # ── BBPS special routing ──────────────────────────────────────────────────
    # BBPS has its own engine/endpoints (auto-detects Moneyart vs Levin operator
    # statements). 'side' = 'internal' (Simplibank dump) or 'bank' (operator stmt).
    if config.partner == "bbps":
        return _trigger_bbps_upload(config, current_user, db)

    # ── E-Value special routing ───────────────────────────────────────────────
    # Only the internal load-in dump is auto-uploadable (covers all accounts).
    # Per-account bank statements stay manual in the E-Value window.
    if config.partner == "evalue":
        return _trigger_evalue_upload(config, current_user, db)

    file_path, expected_name = detect_file_for_today(config)
    if file_path is None:
        raise HTTPException(
            status_code=404,
            detail=f"File not found: {expected_name} in {config.folder_path}"
        )

    # Load file
    try:
        df = load_file_to_dataframe(file_path, expected_name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse file: {e}")

    columns = list(df.columns)
    preset  = _detect_bank_format(config.partner, config.side, columns)
    mapping = preset["mapping"] if preset else {}

    # Fallback to smart defaults if no preset
    if not mapping:
        from core.scheduler import _smart_default_mapping
        mapping = _smart_default_mapping(columns)

    # Resolve column names (case-insensitive).
    # IMPORTANT: values must be STRIPPED so they match df.columns after
    # ingest_dataframe() strips whitespace from column headers (fixes Simplibank
    # columns that have leading/trailing spaces in the raw Excel file).
    col_map = {c.strip().lower(): c.strip() for c in columns}
    mapping_resolved = {}
    for std_field, file_col in mapping.items():
        matched = col_map.get(file_col.strip().lower())
        if matched:
            mapping_resolved[std_field] = matched

    session = UploadSession(
        id=generate_id(),
        user_id=current_user.id,
        partner=config.partner,
        side=config.side,
        original_filename=expected_name,
        stored_filename=expected_name,
        recon_date="auto" if config.recon_date_mode == "auto" else datetime.date.today().strftime("%Y-%m-%d"),
        status=UploadStatus.processing,
        row_count=0,
        column_mapping=json.dumps(mapping_resolved),
    )
    db.add(session)
    db.commit()

    # Resolve source_filter from preset (for Axis/Levin internal dumps that share
    # a mixed Simplibank dump file — the preset declares which Source value to keep)
    _source_filter = preset.get("source_filter", "") if preset else ""
    _source_col    = ""
    if _source_filter:
        # Find the actual 'Source' column name in the DataFrame (may be cased differently)
        _source_col = next((c for c in columns if c.strip().lower() == 'source'), "")

    result = ingest_dataframe(
        df=df,
        session=session,
        mapping_dict=mapping_resolved,
        db=db,
        user_id=current_user.id,
        username=current_user.username,
        filter_column=_source_col,
        filter_value=_source_filter,
        do_auto_recon=config.auto_recon,
    )

    # Update config last trigger info
    config.last_triggered_at    = datetime.datetime.utcnow()
    config.last_trigger_status  = "success"
    config.last_trigger_message = f"Ingested {result['row_count']} rows from {expected_name}"
    config.last_trigger_filename = expected_name
    db.commit()

    return {
        **result,
        "filename":       expected_name,
        "format_detected": preset["label"] if preset else "auto-detected",
    }


# ─── Schedule routes ───────────────────────────────────────────────────────────

@router.get("/schedules")
def list_schedules(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    scheds = db.query(UploadSchedule).all()
    return [
        {
            "id":             s.id,
            "watch_folder_id": s.watch_folder_id,
            "hour":           s.hour,
            "minute":         s.minute,
            "is_enabled":     s.is_enabled,
            "last_run_at":    str(s.last_run_at) if s.last_run_at else None,
            "last_run_status": s.last_run_status,
            "last_run_message": s.last_run_message,
        }
        for s in scheds
    ]


@router.put("/schedules/{config_id}")
def upsert_schedule(
    config_id: str,
    body: ScheduleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("upload")),
):
    from core.scheduler import register_job, unregister_job

    config = db.query(WatchFolderConfig).filter(WatchFolderConfig.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")

    if body.hour < 0 or body.hour > 23:
        raise HTTPException(status_code=400, detail="hour must be 0–23")
    if body.minute < 0 or body.minute > 59:
        raise HTTPException(status_code=400, detail="minute must be 0–59")

    sched = db.query(UploadSchedule).filter(UploadSchedule.watch_folder_id == config_id).first()
    if not sched:
        sched = UploadSchedule(id=generate_id(), watch_folder_id=config_id)
        db.add(sched)

    sched.hour       = body.hour
    sched.minute     = body.minute
    sched.is_enabled = body.is_enabled
    db.commit()

    if body.is_enabled:
        register_job(config_id, body.hour, body.minute)
    else:
        unregister_job(config_id)

    return {
        "id":         sched.id,
        "config_id":  config_id,
        "hour":       sched.hour,
        "minute":     sched.minute,
        "is_enabled": sched.is_enabled,
        "message":    f"Schedule {'enabled' if body.is_enabled else 'disabled'} — "
                      f"{'runs daily at ' + str(body.hour).zfill(2) + ':' + str(body.minute).zfill(2) + ' IST' if body.is_enabled else 'no scheduled runs'}",
    }


@router.post("/schedules/{config_id}/toggle")
def toggle_schedule(
    config_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("upload")),
):
    from core.scheduler import register_job, unregister_job

    sched = db.query(UploadSchedule).filter(UploadSchedule.watch_folder_id == config_id).first()
    if not sched:
        raise HTTPException(status_code=404, detail="No schedule configured for this upload type")

    sched.is_enabled = not sched.is_enabled
    db.commit()

    if sched.is_enabled:
        register_job(config_id, sched.hour, sched.minute)
    else:
        unregister_job(config_id)

    return {"is_enabled": sched.is_enabled, "hour": sched.hour, "minute": sched.minute}


# ── SBI Kiosk auto-upload helper ──────────────────────────────────────────────

# Maps the 'side' field used in WatchFolderConfig to the SBI upload endpoint path
_SBI_SIDE_ENDPOINT = {
    "bank_statement":  "/api/sbi/upload/bank-statement",
    "ko_limits":       "/api/sbi/upload/ko-limits",
    "txn_report":      "/api/sbi/upload/txn-report",
    "ko_cash_holding": "/api/sbi/upload/ko-cash-holding",
    "limit_failures":  "/api/sbi/upload/limit-failures",
    "csp_master":      "/api/sbi/upload/csp-master",
}

def _trigger_sbi_upload(config, current_user, db):
    """
    SBI Kiosk auto-upload: find today's file in the configured folder and
    call the appropriate /api/sbi/upload/* handler directly (bypassing the
    standard ingest_dataframe path which doesn't know about SBI file types).
    """
    from core.ingest_service import detect_file_for_today
    from fastapi import UploadFile
    import io

    endpoint = _SBI_SIDE_ENDPOINT.get(config.side)
    if not endpoint:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown SBI file type '{config.side}'. "
                   f"Valid types: {list(_SBI_SIDE_ENDPOINT.keys())}"
        )

    file_path, expected_name = detect_file_for_today(config)
    if file_path is None:
        config.last_triggered_at   = datetime.datetime.utcnow()
        config.last_trigger_status = "not_found"
        db.commit()
        raise HTTPException(
            status_code=404,
            detail=f"File not found: {expected_name} in {config.folder_path}"
        )

    # Read the file bytes and call the SBI handler directly
    try:
        with open(file_path, "rb") as f:
            file_bytes = f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot read file: {e}")

    # Route to the correct SBI upload function
    from routes.sbi_kiosk import (
        upload_bank_statement, upload_ko_limits, upload_txn_report,
        upload_ko_cash_holding, upload_limit_failures, upload_csp_master,
    )

    # Build a minimal UploadFile-like object the handlers expect
    class _FileObj:
        filename = expected_name
        async def read(self): return file_bytes
        class file:
            @staticmethod
            def read(): return file_bytes

    fake_file = _FileObj()

    handler_map = {
        "bank_statement":  upload_bank_statement,
        "ko_limits":       upload_ko_limits,
        "txn_report":      upload_txn_report,
        "ko_cash_holding": upload_ko_cash_holding,
        "limit_failures":  upload_limit_failures,
        "csp_master":      upload_csp_master,
    }
    handler = handler_map[config.side]

    try:
        import asyncio
        # The bank_statement handler is async; others are sync
        if asyncio.iscoroutinefunction(handler):
            result = asyncio.get_event_loop().run_until_complete(
                handler(file=fake_file, force=force, db=db, current_user=current_user)
            )
        else:
            result = handler(file=fake_file, force=force, db=db, current_user=current_user)
    except HTTPException as e:
        # Preserve the real status — a guard 409 [DUPLICATE] must reach the client as a
        # 409 (so the UI can offer Re-upload/replace), not be flattened into a 500.
        config.last_triggered_at   = datetime.datetime.utcnow()
        config.last_trigger_status = "duplicate" if e.status_code == 409 else "error"
        config.last_trigger_message = str(e.detail)[:500]
        db.commit()
        raise
    except Exception as e:
        config.last_triggered_at   = datetime.datetime.utcnow()
        config.last_trigger_status = "error"
        config.last_trigger_message = str(e)[:500]
        db.commit()
        raise HTTPException(status_code=500, detail=f"SBI upload failed: {e}")

    config.last_triggered_at   = datetime.datetime.utcnow()
    config.last_trigger_status = "success"
    config.last_trigger_message = f"Inserted {result.get('inserted', 0)} rows"
    db.commit()

    return {
        "status":   "success",
        "filename": expected_name,
        "endpoint": endpoint,
        **result,
    }


def _trigger_bbps_upload(config, current_user, db):
    """
    BBPS auto-upload: find today's file and call the BBPS upload handler.
    config.side = 'internal' (Simplibank dump) | 'bank' (operator statement,
    provider auto-detected).
    """
    from core.ingest_service import detect_file_for_today
    import asyncio

    if config.side not in ("internal", "bank"):
        raise HTTPException(status_code=400,
            detail=f"BBPS side must be 'internal' or 'bank', got '{config.side}'")

    file_path, expected_name = detect_file_for_today(config)
    if file_path is None:
        config.last_triggered_at   = datetime.datetime.utcnow()
        config.last_trigger_status = "not_found"
        db.commit()
        raise HTTPException(status_code=404,
            detail=f"File not found: {expected_name} in {config.folder_path}")

    try:
        with open(file_path, "rb") as f:
            file_bytes = f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot read file: {e}")

    from routes.bbps import upload_internal, upload_bank

    class _FileObj:
        filename = expected_name
        async def read(self): return file_bytes

    fake_file = _FileObj()
    handler = upload_internal if config.side == "internal" else upload_bank

    try:
        # routes.bbps upload handlers' auth param is `user=`, not `current_user=`.
        result = asyncio.get_event_loop().run_until_complete(
            handler(file=fake_file, force=force, db=db, user=current_user)
        )
    except HTTPException as e:
        config.last_triggered_at   = datetime.datetime.utcnow()
        config.last_trigger_status = "duplicate" if e.status_code == 409 else "error"
        config.last_trigger_message = str(e.detail)[:500]
        db.commit()
        raise
    except Exception as e:
        config.last_triggered_at   = datetime.datetime.utcnow()
        config.last_trigger_status = "error"
        config.last_trigger_message = str(e)[:500]
        db.commit()
        raise HTTPException(status_code=500, detail=f"BBPS upload failed: {e}")

    config.last_triggered_at   = datetime.datetime.utcnow()
    config.last_trigger_status = "success"
    config.last_trigger_message = f"Ingested {result.get('rows', 0)} rows"
    db.commit()

    return {"status": "success", "filename": expected_name, **result}


def _trigger_evalue_upload(config, current_user, db):
    """
    E-Value auto-upload: only the internal load-in dump (side='internal') is
    auto-uploadable. Bank statements are per-account and stay manual.
    """
    from core.ingest_service import detect_file_for_today
    import asyncio

    if config.side != "internal":
        raise HTTPException(status_code=400,
            detail="Only the E-Value internal load-in dump can be auto-uploaded; "
                   "bank statements are uploaded per-account in the E-Value window.")

    file_path, expected_name = detect_file_for_today(config)
    if file_path is None:
        config.last_triggered_at   = datetime.datetime.utcnow()
        config.last_trigger_status = "not_found"
        db.commit()
        raise HTTPException(status_code=404,
            detail=f"File not found: {expected_name} in {config.folder_path}")

    try:
        with open(file_path, "rb") as f:
            file_bytes = f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot read file: {e}")

    from routes.evalue import upload_internal

    class _FileObj:
        filename = expected_name
        async def read(self): return file_bytes

    fake_file = _FileObj()
    try:
        # routes.evalue.upload_internal's auth param is `user=`, not `current_user=` —
        # passing current_user= raised TypeError, so E-Value auto-upload always failed.
        result = asyncio.get_event_loop().run_until_complete(
            upload_internal(file=fake_file, force=force, db=db, user=current_user)
        )
    except HTTPException as e:
        config.last_triggered_at   = datetime.datetime.utcnow()
        config.last_trigger_status = "duplicate" if e.status_code == 409 else "error"
        config.last_trigger_message = str(e.detail)[:500]
        db.commit()
        raise
    except Exception as e:
        config.last_triggered_at   = datetime.datetime.utcnow()
        config.last_trigger_status = "error"
        config.last_trigger_message = str(e)[:500]
        db.commit()
        raise HTTPException(status_code=500, detail=f"E-Value upload failed: {e}")

    config.last_triggered_at   = datetime.datetime.utcnow()
    config.last_trigger_status = "success"
    config.last_trigger_message = f"Ingested {result.get('rows', 0)} rows"
    db.commit()

    return {"status": "success", "filename": expected_name, **result}
