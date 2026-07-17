"""
routes/recycle_bin.py — view and restore data removed by a Clear action.

Deleting recon rows used to be terminal (recovery meant a DB dump). Clear now captures rows
into `recycle_bin` first; this router exposes them so an accidental clear is undone from the
UI in seconds instead of a restore-from-backup.

Gated by the same 'clear_data' permission that deletes — whoever can delete can undo.
Read-only over the recon data itself: a restore re-inserts the ORIGINAL rows unchanged and
never touches matching logic.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from models.database import get_db, User, AuditLog
from core.auth import require_permission
from core import recycle_bin as rb

logger = logging.getLogger("eko_recon.recycle_bin_api")
router = APIRouter(prefix="/api/recycle-bin", tags=["Recycle Bin"])


@router.get("")
def list_bin(
    limit: int = Query(100, le=500),
    include_restored: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("clear_data")),
):
    """Every Clear action still held in the bin, newest first (one entry per action)."""
    return {"batches": rb.list_batches(db, limit=limit, include_restored=include_restored),
            "retention_days": rb.RETAIN_DAYS}


@router.post("/{batch_id}/restore")
def restore(
    batch_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("clear_data")),
):
    """Put a cleared batch back. Idempotent — rows that already exist are skipped."""
    try:
        res = rb.restore_batch(db, batch_id, current_user.username)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("recycle_bin restore failed for %s", batch_id)
        raise HTTPException(status_code=500, detail=f"Restore failed: {e}")
    try:
        db.add(AuditLog(user_id=current_user.id, username=current_user.username,
                        action="recycle_restore", action_type="human", entity_type="module",
                        entity_id=batch_id[:36],
                        detail=__import__("json").dumps(res)))
        db.commit()
    except Exception:
        db.rollback()
    return res


@router.delete("/{batch_id}")
def purge(
    batch_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("clear_data")),
):
    """Permanently drop a batch from the bin. The rows are already deleted — this only
    discards the ability to restore them."""
    n = rb.purge_batch(db, batch_id)
    if not n:
        raise HTTPException(status_code=404, detail="No such batch in the recycle bin")
    return {"purged_entries": n, "batch_id": batch_id}
