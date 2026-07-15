"""
file_hash_guard.py — central duplicate-file protection for the dedicated modules
(E-Value, BBPS, SBI…). The core engine already hard-blocks re-uploads via
UploadSession; the modules ingest directly, so this guard stores a SHA-256 of
every ingested file and rejects the exact same bytes a second time.
"""
import hashlib

from fastapi import HTTPException


def guard_duplicate_file(db, module: str, side: str, file_bytes: bytes,
                         filename: str, user=None, force: bool = False) -> str:
    """
    Raise 409 if this exact file was already ingested for `module`.
    Otherwise record its hash (committed together with the caller's ingest)
    and return the hex digest.

    `force=True` bypasses the 409 and re-allows the same bytes. This exists for the
    E-Value BANK path only: the hash records a *file ever uploaded*, not *data still
    present*, and bank rows can be removed later by a subsequent per-date replace (or a
    clear), yet this hash lingers — so a legitimate re-upload to RESTORE that data would
    otherwise be blocked forever. The bank re-upload is idempotent (per-date replace,
    preserving matched/SRC rows), so re-applying only rewrites the ordinary rows for
    those dates. Do NOT pass force on the internal-dump path: it upserts per eko_trxn_id
    and never bulk-deletes, so its data cannot be lost, and rows with a blank
    eko_trxn_id are appended (not upserted) — a forced replay would duplicate them.
    The hash stays on record (audit trail); force just declines to hard-error on it.
    """
    from models.database import ModuleUploadHash

    sha = hashlib.sha256(file_bytes).hexdigest()
    existing = db.query(ModuleUploadHash).filter(
        ModuleUploadHash.module == module,
        ModuleUploadHash.sha256 == sha,
    ).first()
    if existing:
        if not force:
            when = existing.uploaded_at.strftime("%Y-%m-%d %H:%M UTC") if existing.uploaded_at else "earlier"
            raise HTTPException(
                status_code=409,
                detail=(f"[DUPLICATE] This exact file was already uploaded to {module} "
                        f"as '{existing.filename}' on {when}. "
                        f"If the source data changed, upload the corrected file (its content differs, so it will pass) "
                        f"— or use Re-upload (replace) if you are restoring rows that were removed since."),
            )
        return sha
    db.add(ModuleUploadHash(
        module=module, side=side, sha256=sha, filename=filename,
        uploaded_by=getattr(user, "username", None),
    ))
    return sha
