"""
routes/recon_jobs.py

Performance/scalability endpoints (additive — the existing sync recon and
offset-paginated open-items endpoints are unchanged):

  • Background reconciliation        POST /api/recon/run-async    + GET /api/recon/job/{id}
  • Keyset (cursor) pagination       GET  /api/recon/open-items-keyset
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional
from pydantic import BaseModel

from models.database import get_db, SessionLocal, Transaction, ReconStatus, ReconRun, User
from core.auth import get_current_user, require_permission
from core import jobs

logger = logging.getLogger("eko_recon")
router = APIRouter(prefix="/api/recon", tags=["recon-jobs"])


# ── Background reconciliation ─────────────────────────────────────────────────
class RunAsyncIn(BaseModel):
    partner: str
    recon_date: Optional[str] = None        # single date
    from_date: Optional[str] = None          # or a range
    to_date: Optional[str] = None


def _run_recon_worker(partner: str, dates: list, user_id: str) -> dict:
    """Runs in a background thread with its own DB session."""
    from core.matching_engine import run_reconciliation, run_bank_reversal_match
    db = SessionLocal()
    try:
        out = {"partner": partner, "dates": [], "total_matched": 0}
        for d in dates:
            r = run_reconciliation(partner, d, db, user_id)
            try:
                run_bank_reversal_match(partner, d, db, user_id)  # self-guarded; nets refund round trips
            except Exception:
                pass
            out["dates"].append({"date": d, "matched": r.get("matched", 0)})
            out["total_matched"] += r.get("matched", 0)
        return out
    finally:
        db.close()


@router.post("/run-async")
def run_async(body: RunAsyncIn, db: Session = Depends(get_db),
              user: User = Depends(require_permission("run_recon"))):
    """Submit a (possibly large) reconciliation to the background worker."""
    if body.recon_date:
        dates = [body.recon_date]
    elif body.from_date and body.to_date:
        rows = db.query(Transaction.recon_date).filter(
            Transaction.partner == body.partner,
            Transaction.recon_date >= body.from_date,
            Transaction.recon_date <= body.to_date,
        ).distinct().all()
        dates = sorted({r[0] for r in rows if r[0]})
    else:
        raise HTTPException(400, "Provide recon_date, or from_date + to_date")
    if not dates:
        raise HTTPException(400, "No data found for the given partner/date(s)")
    job_id = jobs.submit("reconciliation", _run_recon_worker, body.partner, dates, user.id,
                         label=f"{body.partner} · {len(dates)} date(s)")
    return {"job_id": job_id, "status": "queued", "dates": len(dates)}


@router.get("/job/{job_id}")
def job_status(job_id: str, user: User = Depends(get_current_user)):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Job not found (it may have expired)")
    return j


@router.get("/jobs")
def list_jobs(user: User = Depends(get_current_user)):
    return {"jobs": jobs.recent(50)}


# ── Keyset (cursor) pagination for open items ─────────────────────────────────
@router.get("/open-items-keyset")
def open_items_keyset(
    partner: Optional[str] = None,
    side: Optional[str] = None,
    recon_status: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    cursor: Optional[str] = Query(None, description="Last id from the previous page"),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Cursor-based (keyset) pagination — constant-time per page regardless of depth,
    unlike LIMIT/OFFSET which degrades on deep pages. Stable order by primary key.
    Pass the returned `next_cursor` to fetch the following page.
    """
    q = db.query(Transaction).filter(Transaction.row_type == "txn")
    if partner:      q = q.filter(Transaction.partner == partner)
    if side:         q = q.filter(Transaction.side == side)
    if from_date:    q = q.filter(Transaction.recon_date >= from_date)
    if to_date:      q = q.filter(Transaction.recon_date <= to_date)
    if recon_status == "open":
        q = q.filter(Transaction.recon_status.in_([ReconStatus.unmatched, ReconStatus.src_assigned]))
    elif recon_status:
        q = q.filter(Transaction.recon_status == recon_status)
    else:
        q = q.filter(Transaction.recon_status == ReconStatus.unmatched)

    if cursor:
        q = q.filter(Transaction.id > cursor)
    rows = q.order_by(Transaction.id).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [{
            "id": t.id, "partner": t.partner, "side": t.side, "recon_date": t.recon_date,
            "eko_tid": t.eko_tid, "tracking_number": t.tracking_number, "utr_number": t.utr_number,
            "amount": t.amount, "dr_cr": t.dr_cr, "recon_status": t.recon_status,
            "match_id": t.match_id, "src_code": t.src_code,
        } for t in rows],
        "count": len(rows),
        "next_cursor": rows[-1].id if (rows and has_more) else None,
        "has_more": has_more,
    }
