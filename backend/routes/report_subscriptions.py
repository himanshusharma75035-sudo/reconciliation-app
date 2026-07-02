"""
routes/report_subscriptions.py

Personal scheduled report subscriptions.
Every user manages their own; admins can see all.
"""
import json, io, datetime, logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import Optional
from pydantic import BaseModel
from models.database import get_db, User, ReportSubscription, generate_id
from core.auth import get_current_user

logger = logging.getLogger("eko_recon")
router = APIRouter(prefix="/api/report-subscriptions", tags=["report-subscriptions"])

VALID_REPORT_TYPES = ["open_items", "summary", "eod", "matched_pairs", "ageing", "src",
                      "evalue_summary", "evalue_exceptions", "evalue_unmatched",
                      "bbps_summary", "bbps_exceptions", "bbps_unmatched",
                      "funds_position"]
VALID_FREQUENCIES  = ["daily", "weekly", "monthly"]
VALID_DATE_RANGES  = ["today", "yesterday", "last_7_days", "this_week", "this_month", "last_month"]


# ─── Pydantic schemas ─────────────────────────────────────────────────────────
class SubscriptionIn(BaseModel):
    name:          str
    report_type:   str
    filters:       dict = {}
    date_range:    str  = "yesterday"
    frequency:     str  = "daily"
    hour:          int  = 8
    minute:        int  = 0
    day_of_week:   Optional[int] = None
    day_of_month:  Optional[int] = None
    email_to:      Optional[str] = None


# ─── CRUD endpoints ───────────────────────────────────────────────────────────
@router.get("")
def list_subscriptions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all subscriptions for the current user (admin sees all)."""
    q = db.query(ReportSubscription)
    if current_user.role != "admin":
        q = q.filter(ReportSubscription.user_id == current_user.id)
    subs = q.order_by(ReportSubscription.created_at.desc()).all()
    return [_to_dict(s) for s in subs]


@router.post("")
def create_subscription(
    body: SubscriptionIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new personal scheduled report."""
    if body.report_type not in VALID_REPORT_TYPES:
        raise HTTPException(400, f"Invalid report_type. Valid: {VALID_REPORT_TYPES}")
    if body.frequency not in VALID_FREQUENCIES:
        raise HTTPException(400, f"Invalid frequency. Valid: {VALID_FREQUENCIES}")
    if body.date_range not in VALID_DATE_RANGES:
        raise HTTPException(400, f"Invalid date_range. Valid: {VALID_DATE_RANGES}")
    if not (0 <= body.hour <= 23) or not (0 <= body.minute <= 59):
        raise HTTPException(400, "Hour must be 0-23, minute must be 0-59")

    sub = ReportSubscription(
        id           = generate_id(),
        user_id      = current_user.id,
        username     = current_user.username,
        name         = body.name.strip(),
        report_type  = body.report_type,
        filters      = json.dumps(body.filters),
        date_range   = body.date_range,
        frequency    = body.frequency,
        hour         = body.hour,
        minute       = body.minute,
        day_of_week  = body.day_of_week,
        day_of_month = body.day_of_month,
        email_to     = body.email_to or getattr(current_user, "email", None),
    )
    db.add(sub)
    db.commit()

    # Register the cron job immediately
    from core.report_scheduler import register_report_job
    register_report_job(sub)

    return _to_dict(sub)


@router.put("/{sub_id}")
def update_subscription(
    sub_id: str,
    body: SubscriptionIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub = _get_own(sub_id, current_user, db)
    sub.name         = body.name.strip()
    sub.report_type  = body.report_type
    sub.filters      = json.dumps(body.filters)
    sub.date_range   = body.date_range
    sub.frequency    = body.frequency
    sub.hour         = body.hour
    sub.minute       = body.minute
    sub.day_of_week  = body.day_of_week
    sub.day_of_month = body.day_of_month
    if body.email_to is not None:
        sub.email_to = body.email_to
    db.commit()

    from core.report_scheduler import register_report_job
    register_report_job(sub)
    return _to_dict(sub)


@router.patch("/{sub_id}/toggle")
def toggle_subscription(
    sub_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub = _get_own(sub_id, current_user, db)
    sub.is_active = not sub.is_active
    db.commit()
    from core.report_scheduler import register_report_job, unregister_report_job
    if sub.is_active:
        register_report_job(sub)
    else:
        unregister_report_job(sub.id)
    return _to_dict(sub)


@router.delete("/{sub_id}")
def delete_subscription(
    sub_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub = _get_own(sub_id, current_user, db)
    from core.report_scheduler import unregister_report_job
    unregister_report_job(sub.id)
    db.delete(sub)
    db.commit()
    return {"message": "Deleted"}


@router.post("/{sub_id}/run-now")
def run_now(
    sub_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Run a subscription report immediately (for testing / on-demand)."""
    sub = _get_own(sub_id, current_user, db)
    from core.report_scheduler import execute_report_subscription
    try:
        result = execute_report_subscription(sub.id)
        return {"message": "Report sent", "detail": result}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _get_own(sub_id: str, user: User, db: Session) -> ReportSubscription:
    sub = db.query(ReportSubscription).filter(ReportSubscription.id == sub_id).first()
    if not sub:
        raise HTTPException(404, "Subscription not found")
    if user.role != "admin" and sub.user_id != user.id:
        raise HTTPException(403, "Not your subscription")
    return sub


def _to_dict(s: ReportSubscription) -> dict:
    return {
        "id":              s.id,
        "user_id":         s.user_id,
        "username":        s.username,
        "name":            s.name,
        "report_type":     s.report_type,
        "filters":         json.loads(s.filters or "{}"),
        "date_range":      s.date_range,
        "frequency":       s.frequency,
        "hour":            s.hour,
        "minute":          s.minute,
        "day_of_week":     s.day_of_week,
        "day_of_month":    s.day_of_month,
        "email_to":        s.email_to,
        "is_active":       s.is_active,
        "last_run_at":     str(s.last_run_at) if s.last_run_at else None,
        "last_run_status": s.last_run_status,
        "last_run_message":s.last_run_message,
        "created_at":      str(s.created_at),
    }
