"""
workflow.py — Tier 1 operations layer:
  • Exception workflow  : assign open items to owners with reason codes
  • Maker-checker       : approval queue for manual actions (+ settings toggle)
  • Fee & commission    : expected-fee rules per partner + verification report
  • Suggested matches   : ranked likely bank↔internal pairs for one-click confirm
  • Anomaly detection   : duplicate references + volume spikes (rule-based)
"""
import json
import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from models.database import (
    get_db, Transaction, ReconStatus, User, ApprovalRequest, FeeRule,
    SystemSetting, generate_id,
)
from core.auth import get_current_user, require_permission, require_admin
from core import maker_checker
from routes.recon import _log

router = APIRouter(prefix="/api/workflow", tags=["workflow"])

# Reason codes for exception assignment — fixed vocabulary so reporting stays clean.
EXCEPTION_REASONS = [
    "BANK_DELAY", "MISSING_UTR", "AMOUNT_DIFF", "DUPLICATE",
    "REFUND_PENDING", "OPERATOR_ISSUE", "TECH_ISSUE", "OTHER",
]


# ═══════════════════════════════ Exceptions ══════════════════════════════════

class AssignRequest(BaseModel):
    transaction_ids: List[str]
    assigned_to: str            # username ("" = unassign)
    reason: Optional[str] = None


@router.get("/assignees")
def list_assignees(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Active usernames for the assignment dropdown (names only — not admin data)."""
    rows = db.query(User.username).filter(User.is_active == True).order_by(User.username).all()  # noqa: E712
    return {"assignees": [r[0] for r in rows], "reasons": EXCEPTION_REASONS}


@router.post("/assign")
def assign_exceptions(req: AssignRequest, db: Session = Depends(get_db),
                      user=Depends(get_current_user)):
    if not req.transaction_ids:
        raise HTTPException(400, "No transaction IDs provided")
    if req.reason and req.reason not in EXCEPTION_REASONS:
        raise HTTPException(400, f"Invalid reason. Allowed: {EXCEPTION_REASONS}")
    if req.assigned_to:
        target = db.query(User).filter(User.username == req.assigned_to, User.is_active == True).first()  # noqa: E712
        if not target:
            raise HTTPException(404, f"User '{req.assigned_to}' not found or inactive")

    txns = db.query(Transaction).filter(Transaction.id.in_(req.transaction_ids)).all()
    now = datetime.datetime.utcnow()
    for t in txns:
        t.assigned_to = req.assigned_to or None
        t.assigned_at = now if req.assigned_to else None
        if req.reason:
            t.exception_reason = req.reason
    db.commit()
    _log(db, user, "assign_exception", "transaction", detail={
        "count": len(txns), "assigned_to": req.assigned_to, "reason": req.reason,
        "transaction_ids": req.transaction_ids[:20],
    })
    return {"updated": len(txns), "assigned_to": req.assigned_to or None}


@router.get("/queue-stats")
def queue_stats(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Open items per owner — drives the exception-queue overview."""
    rows = db.query(Transaction.assigned_to, func.count(Transaction.id)) \
        .filter(Transaction.recon_status.in_([ReconStatus.unmatched, ReconStatus.src_assigned])) \
        .group_by(Transaction.assigned_to).all()
    out = {"unassigned": 0, "by_user": {}}
    for (owner, cnt) in rows:
        if owner:
            out["by_user"][owner] = cnt
        else:
            out["unassigned"] = cnt
    return out


# ═══════════════════════════════ Maker-checker ═══════════════════════════════

@router.get("/settings")
def get_settings(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return {"maker_checker_enabled": maker_checker.is_enabled(db)}


class SettingUpdate(BaseModel):
    enabled: bool


@router.put("/settings/maker-checker")
def update_maker_checker(body: SettingUpdate, db: Session = Depends(get_db),
                         user=Depends(require_admin)):
    maker_checker.set_enabled(db, body.enabled, user.username)
    _log(db, user, "maker_checker_toggle", "system_setting", detail={"enabled": body.enabled})
    return {"maker_checker_enabled": body.enabled}


@router.get("/approvals")
def list_approvals(status: str = Query("pending"),
                   db: Session = Depends(get_db), user=Depends(get_current_user)):
    q = db.query(ApprovalRequest)
    if status and status != "all":
        q = q.filter(ApprovalRequest.status == status)
    rows = q.order_by(ApprovalRequest.requested_at.desc()).limit(300).all()
    return [{
        "id": r.id, "action_type": r.action_type, "summary": r.summary,
        "partner": r.partner, "payload": json.loads(r.payload or "{}"),
        "requested_by": r.requested_by,
        "requested_at": r.requested_at.isoformat() if r.requested_at else None,
        "status": r.status, "reviewed_by": r.reviewed_by,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        "review_note": r.review_note,
    } for r in rows]


class ReviewBody(BaseModel):
    note: Optional[str] = ""


def _execute_approved(req: ApprovalRequest, db: Session, approver: User) -> dict:
    """Run the queued action as the approver, inside a maker-checker REPLAY context so
    the replayed endpoints (core AND product routers self-intercept) EXECUTE directly
    instead of re-queuing. Required because 'approver' is a grantable permission on
    non-admin users, so the admin bypass alone would drop the action and re-queue it."""
    from core import maker_checker as _mc
    _tok = _mc.begin_replay()
    try:
        return _dispatch_approved(req, db, approver)
    finally:
        _mc.end_replay(_tok)


def _dispatch_approved(req: ApprovalRequest, db: Session, approver: User) -> dict:
    """Reconstruct and run the approved action. Lazy imports avoid circular deps."""
    p = json.loads(req.payload or "{}")
    from routes.recon import (
        override_status, bulk_override_status, do_assign_src,
        OverrideStatusRequest, BulkOverrideRequest, SRCRequest,
    )
    from core.matching_engine import manual_match as engine_manual_match

    if req.action_type == "override":
        return override_status(OverrideStatusRequest(**p), db=db, current_user=approver)
    if req.action_type == "bulk_override":
        return bulk_override_status(BulkOverrideRequest(**p), db=db, current_user=approver)
    if req.action_type == "src_assign":
        return do_assign_src(SRCRequest(**p), db=db, current_user=approver)
    if req.action_type == "manual_match":
        result = engine_manual_match(p["bank_txn_id"], p["internal_txn_id"], db)
        _log(db, approver, "manual_match", "transaction", p["bank_txn_id"], result)
        return result
    if req.action_type == "unmatch":
        from routes.recon import unmatch_pair, UnmatchRequest
        return unmatch_pair(UnmatchRequest(**p), db=db, current_user=approver)
    if req.action_type == "delete_txns":
        from routes.upload import clear_selected
        return clear_selected(transaction_ids=p["transaction_ids"], db=db, current_user=approver)

    # ── Product-router actions (E-Value / BBPS / SBI Kiosk) ──────────────────────
    # Replay the queued product action AS the approver. The product endpoints call
    # maker_checker.intercept() themselves, but the approver is a checker (admin), so
    # intercept returns None and the action executes directly — parity with the core
    # replay above. Body-less endpoints (unmatch / bulk-unmatch / delete) are replayed
    # with their original query/path args from the payload.
    if req.action_type.startswith("evalue_"):
        import routes.evalue as EV
        v = req.action_type[len("evalue_"):]
        if v == "manual_match":
            return EV.manual_match(EV.ManualMatchIn(**p), db=db, user=approver)
        if v == "unmatch":
            return EV.unmatch(match_id=p.get("match_id"), bank_txn_id=p.get("bank_txn_id"),
                              load_id=p.get("load_id"), db=db, user=approver)
        if v == "override":
            return EV.override_status(EV.OverrideIn(**p), db=db, user=approver)
        if v == "assign_src":
            return EV.assign_src(EV.EvalueSRCIn(**p), db=db, user=approver)
        if v == "assign_src_bulk":
            return EV.assign_src_bulk(EV.EvalueBulkSRCIn(**p), db=db, user=approver)
        if v == "bulk_override":
            return EV.bulk_override(EV.BulkOverrideIn(**p), db=db, user=approver)
        if v == "bulk_unmatch":
            return EV.bulk_unmatch(match_ids=p.get("match_ids"), db=db, user=approver)
        if v == "interbank_match":
            return EV.interbank_manual(EV.IbtManualIn(**p), db=db, user=approver)
    if req.action_type.startswith("bbps_"):
        import routes.bbps as BP
        v = req.action_type[len("bbps_"):]
        if v == "manual_match":
            return BP.manual_match(BP.ManualMatchIn(**p), db=db, user=approver)
        if v == "unmatch":
            return BP.unmatch(match_id=p.get("match_id"), db=db, user=approver)
        if v == "override":
            return BP.override_status(BP.OverrideIn(**p), db=db, user=approver)
        if v == "assign_src":
            return BP.assign_src(BP.BbpsSRCIn(**p), db=db, user=approver)
        if v == "assign_src_bulk":
            return BP.assign_src_bulk(BP.BbpsBulkSRCIn(**p), db=db, user=approver)
    if req.action_type == "sbi_manual_match":
        import routes.sbi_kiosk as SK
        return SK.create_manual_match(SK.ManualMatchIn(**p), db=db, current_user=approver)
    if req.action_type == "sbi_delete_manual_match":
        import routes.sbi_kiosk as SK
        return SK.delete_manual_match(p["mm_id"], db=db, current_user=approver)
    if req.action_type == "sbi_manual_pair":
        import routes.sbi_kiosk as SK
        return SK.create_manual_pairs(SK.ManualPairBulkIn(**p), db=db, current_user=approver)
    if req.action_type == "sbi_delete_manual_pair":
        import routes.sbi_kiosk as SK
        return SK.delete_manual_pair(p["pair_id"], db=db, current_user=approver)

    raise HTTPException(400, f"Unknown action type '{req.action_type}'")


@router.post("/approvals/{approval_id}/approve")
def approve(approval_id: str, body: ReviewBody,
            db: Session = Depends(get_db),
            user=Depends(require_permission("approver"))):
    req = db.query(ApprovalRequest).filter(ApprovalRequest.id == approval_id).first()
    if not req:
        raise HTTPException(404, "Approval request not found")
    if req.status != "pending":
        raise HTTPException(400, f"Request already {req.status}")
    if req.requested_by == user.username:
        raise HTTPException(403, "Maker-checker: you cannot approve your own request")

    result = _execute_approved(req, db, user)
    req.status = "approved"
    req.reviewed_by = user.username
    req.reviewed_at = datetime.datetime.utcnow()
    req.review_note = (body.note or "")[:400]
    db.commit()
    _log(db, user, "approval_approved", "approval_request", req.id,
         detail={"action_type": req.action_type, "requested_by": req.requested_by})
    return {"status": "approved", "result": result}


@router.post("/approvals/{approval_id}/reject")
def reject(approval_id: str, body: ReviewBody,
           db: Session = Depends(get_db),
           user=Depends(require_permission("approver"))):
    req = db.query(ApprovalRequest).filter(ApprovalRequest.id == approval_id).first()
    if not req:
        raise HTTPException(404, "Approval request not found")
    if req.status != "pending":
        raise HTTPException(400, f"Request already {req.status}")
    if not (body.note or "").strip():
        raise HTTPException(400, "A note is required when rejecting")
    req.status = "rejected"
    req.reviewed_by = user.username
    req.reviewed_at = datetime.datetime.utcnow()
    req.review_note = body.note[:400]
    db.commit()
    _log(db, user, "approval_rejected", "approval_request", req.id,
         detail={"action_type": req.action_type, "requested_by": req.requested_by, "note": body.note[:200]})
    return {"status": "rejected"}


# ═══════════════════════════════ Fee rules ═══════════════════════════════════

class FeeRuleIn(BaseModel):
    partner: str
    label: str
    fee_type: str = "percent"     # percent | flat
    fee_value: float = 0
    gst_percent: float = 18.0
    tolerance: float = 0.5
    is_active: bool = True


def _fee_out(r: FeeRule) -> dict:
    return {"id": r.id, "partner": r.partner, "label": r.label, "fee_type": r.fee_type,
            "fee_value": r.fee_value, "gst_percent": r.gst_percent,
            "tolerance": r.tolerance, "is_active": r.is_active}


@router.get("/fee-rules")
def list_fee_rules(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return [_fee_out(r) for r in db.query(FeeRule).order_by(FeeRule.partner).all()]


@router.post("/fee-rules")
def create_fee_rule(body: FeeRuleIn, db: Session = Depends(get_db),
                    user=Depends(require_admin)):
    if body.fee_type not in ("percent", "flat"):
        raise HTTPException(400, "fee_type must be 'percent' or 'flat'")
    r = FeeRule(**body.dict())
    db.add(r); db.commit()
    _log(db, user, "fee_rule_created", "fee_rule", r.id, detail=body.dict())
    return _fee_out(r)


@router.put("/fee-rules/{rule_id}")
def update_fee_rule(rule_id: str, body: FeeRuleIn, db: Session = Depends(get_db),
                    user=Depends(require_admin)):
    r = db.query(FeeRule).filter(FeeRule.id == rule_id).first()
    if not r:
        raise HTTPException(404, "Fee rule not found")
    for k, v in body.dict().items():
        setattr(r, k, v)
    db.commit()
    _log(db, user, "fee_rule_updated", "fee_rule", r.id, detail=body.dict())
    return _fee_out(r)


@router.delete("/fee-rules/{rule_id}")
def delete_fee_rule(rule_id: str, db: Session = Depends(get_db),
                    user=Depends(require_admin)):
    r = db.query(FeeRule).filter(FeeRule.id == rule_id).first()
    if not r:
        raise HTTPException(404, "Fee rule not found")
    db.delete(r); db.commit()
    _log(db, user, "fee_rule_deleted", "fee_rule", rule_id)
    return {"deleted": rule_id}


@router.get("/verify-fees")
def verify_fees(partner: str = Query(...),
                date_from: str = Query(""), date_to: str = Query(""),
                db: Session = Depends(get_db), user=Depends(get_current_user)):
    """
    Compare bank-side net_amount against amount − expected fee (rule + GST).
    Only rows that carry BOTH amount and net_amount are verifiable.
    """
    rule = db.query(FeeRule).filter(FeeRule.partner == partner, FeeRule.is_active == True).first()  # noqa: E712
    if not rule:
        raise HTTPException(404, f"No active fee rule configured for '{partner}'. Add one in Workflow → Fee Rules.")

    q = db.query(Transaction).filter(
        Transaction.partner == partner,
        Transaction.side == "bank",
        Transaction.amount.isnot(None),
        Transaction.net_amount.isnot(None),
    )
    if date_from: q = q.filter(Transaction.recon_date >= date_from)
    if date_to:   q = q.filter(Transaction.recon_date <= date_to)
    rows = q.limit(20000).all()

    mismatches, ok, total_fee_expected, total_fee_actual = [], 0, 0.0, 0.0
    for t in rows:
        amt = float(t.amount or 0)
        base_fee = amt * float(rule.fee_value) / 100.0 if rule.fee_type == "percent" else float(rule.fee_value)
        expected_fee = base_fee * (1 + float(rule.gst_percent) / 100.0)
        expected_net = amt - expected_fee
        actual_net = float(t.net_amount or 0)
        actual_fee = amt - actual_net
        total_fee_expected += expected_fee
        total_fee_actual += actual_fee
        if abs(actual_net - expected_net) > float(rule.tolerance):
            mismatches.append({
                "id": t.id, "eko_tid": t.eko_tid, "recon_date": t.recon_date,
                "amount": round(amt, 2), "net_amount": round(actual_net, 2),
                "expected_net": round(expected_net, 2),
                "fee_charged": round(actual_fee, 2), "fee_expected": round(expected_fee, 2),
                "diff": round(actual_net - expected_net, 2),
            })
        else:
            ok += 1
    return {
        "rule": _fee_out(rule), "checked": len(rows), "ok": ok,
        "mismatch_count": len(mismatches),
        "total_fee_expected": round(total_fee_expected, 2),
        "total_fee_charged": round(total_fee_actual, 2),
        "overcharge": round(total_fee_actual - total_fee_expected, 2),
        "mismatches": mismatches[:500],
    }


# ═══════════════════════════════ Suggested matches ═══════════════════════════

@router.get("/suggested-matches")
def suggested_matches(partner: str = Query(...),
                      date_from: str = Query(""), date_to: str = Query(""),
                      limit: int = Query(50, le=200),
                      db: Session = Depends(get_db), user=Depends(get_current_user)):
    """
    Heuristic candidate pairs for unmatched rows: same amount (±₹1), dates within
    2 days, plus reference similarity. Scored 0–100; confirm via /recon/manual-match.
    """
    def base(side):
        q = db.query(Transaction).filter(
            Transaction.partner == partner, Transaction.side == side,
            Transaction.recon_status == ReconStatus.unmatched,
            Transaction.row_type == "txn",
        )
        if date_from: q = q.filter(Transaction.recon_date >= date_from)
        if date_to:   q = q.filter(Transaction.recon_date <= date_to)
        return q.limit(3000).all()

    bank_rows, internal_rows = base("bank"), base("internal")
    if not bank_rows or not internal_rows:
        return {"suggestions": [], "bank_unmatched": len(bank_rows), "internal_unmatched": len(internal_rows)}

    # Index internal rows by rounded amount for O(1) candidate lookup
    by_amount = {}
    for t in internal_rows:
        key = round(float(t.amount or 0))
        by_amount.setdefault(key, []).append(t)

    def _days_diff(a, b):
        try:
            da = datetime.date.fromisoformat((a or "")[:10])
            db_ = datetime.date.fromisoformat((b or "")[:10])
            return abs((da - db_).days)
        except Exception:
            return 99

    def _norm(s):
        return "".join(ch for ch in str(s or "").upper() if ch.isalnum())

    suggestions, used_internal = [], set()
    for b in bank_rows:
        amt = float(b.amount or 0)
        candidates = []
        for key in (round(amt) - 1, round(amt), round(amt) + 1):
            candidates.extend(by_amount.get(key, []))
        best, best_score, best_why = None, 0, []
        for i in candidates:
            if i.id in used_internal:
                continue
            score, why = 0, []
            diff = abs(amt - float(i.amount or 0))
            if diff <= 0.01:   score += 50; why.append("exact amount")
            elif diff <= 1.0:  score += 35; why.append(f"amount ±₹{diff:.2f}")
            else:
                continue
            dd = _days_diff(b.transaction_date or b.recon_date, i.transaction_date or i.recon_date)
            if dd == 0:   score += 30; why.append("same day")
            elif dd <= 2: score += 15; why.append(f"{dd}d apart")
            bt, it = _norm(b.eko_tid), _norm(i.eko_tid)
            br, ir = _norm(b.tracking_number), _norm(i.tracking_number)
            if bt and it and (bt in it or it in bt): score += 20; why.append("TID overlap")
            elif br and ir and (br in ir or ir in br): score += 20; why.append("tracking overlap")
            bu, iu = _norm(b.utr_number), _norm(i.utr_number)
            if bu and iu and bu == iu: score += 25; why.append("same UTR")
            if score > best_score:
                best, best_score, best_why = i, score, why
        if best and best_score >= 50:
            used_internal.add(best.id)
            suggestions.append({
                "source": "system",
                "score": min(best_score, 100), "why": best_why,
                "bank": {"id": b.id, "eko_tid": b.eko_tid, "tracking_number": b.tracking_number,
                         "amount": b.amount, "transaction_date": b.transaction_date, "recon_date": b.recon_date},
                "internal": {"id": best.id, "eko_tid": best.eko_tid, "tracking_number": best.tracking_number,
                             "amount": best.amount, "transaction_date": best.transaction_date, "recon_date": best.recon_date},
            })
    suggestions.sort(key=lambda s: -s["score"])
    return {"suggestions": suggestions[:limit],
            "bank_unmatched": len(bank_rows), "internal_unmatched": len(internal_rows)}


@router.get("/suggested-matches-ai")
def suggested_matches_ai(partner: str = Query(...),
                         date_from: str = Query(""), date_to: str = Query(""),
                         limit: int = Query(50, le=200),
                         db: Session = Depends(get_db), user=Depends(get_current_user)):
    """
    AI-assisted candidate pairs for the same unmatched rows the heuristic sees.
    Advisory only: the model receives a DE-IDENTIFIED, allowlist-only payload
    (no account numbers / customer names / narration), refers to rows by opaque
    tokens, and NEVER writes — a human confirms each match via /recon/manual-match.
    Returns the same shape as /suggested-matches, tagged "source": "ai".
    """
    def base(side):
        q = db.query(Transaction).filter(
            Transaction.partner == partner, Transaction.side == side,
            Transaction.recon_status == ReconStatus.unmatched,
            Transaction.row_type == "txn",
        )
        if date_from: q = q.filter(Transaction.recon_date >= date_from)
        if date_to:   q = q.filter(Transaction.recon_date <= date_to)
        return q.limit(3000).all()

    bank_rows, internal_rows = base("bank"), base("internal")
    from core.ai_matcher import suggest_matches
    return suggest_matches(bank_rows, internal_rows, limit=limit)


# ═══════════════════════════════ Rule learning (Tier 2) ══════════════════════

@router.get("/rule-suggestions")
def rule_suggestions(min_hits: int = Query(2), db: Session = Depends(get_db),
                     user=Depends(get_current_user)):
    """Patterns learned from manual matches — repeated ≥ min_hits and not yet a rule."""
    from models.database import RuleSuggestion
    rows = db.query(RuleSuggestion).filter(
        RuleSuggestion.status == "suggested",
        RuleSuggestion.hit_count >= min_hits,
    ).order_by(RuleSuggestion.hit_count.desc()).all()
    return [{
        "id": r.id, "partner": r.partner, "fields": (r.fields_csv or "").split(","),
        "hit_count": r.hit_count, "last_seen": r.last_seen.isoformat() if r.last_seen else None,
    } for r in rows]


@router.post("/rule-suggestions/{sug_id}/accept")
def accept_rule_suggestion(sug_id: str, db: Session = Depends(get_db),
                           user=Depends(require_permission("logic_builder"))):
    """Promote a learned pattern to a real MatchRule (appended at lowest priority)."""
    from models.database import RuleSuggestion, MatchRule
    sug = db.query(RuleSuggestion).filter(RuleSuggestion.id == sug_id).first()
    if not sug:
        raise HTTPException(404, "Suggestion not found")
    if sug.status != "suggested":
        raise HTTPException(400, f"Already {sug.status}")
    fields = [f for f in (sug.fields_csv or "").split(",") if f]
    max_prio = db.query(func.max(MatchRule.priority)).filter(MatchRule.partner == sug.partner).scalar() or 0
    rule = MatchRule(
        name=f"Learned: {' + '.join(f.replace('_', ' ').title() for f in fields)}",
        partner=sug.partner, priority=max_prio + 1,
        match_fields=json.dumps(fields), is_active=True,
    )
    db.add(rule)
    sug.status = "accepted"
    db.commit()
    _log(db, user, "rule_suggestion_accepted", "match_rule", rule.id,
         detail={"partner": sug.partner, "fields": fields, "hits": sug.hit_count})
    return {"created_rule": rule.name, "partner": sug.partner, "priority": rule.priority}


@router.post("/rule-suggestions/{sug_id}/dismiss")
def dismiss_rule_suggestion(sug_id: str, db: Session = Depends(get_db),
                            user=Depends(require_permission("logic_builder"))):
    from models.database import RuleSuggestion
    sug = db.query(RuleSuggestion).filter(RuleSuggestion.id == sug_id).first()
    if not sug:
        raise HTTPException(404, "Suggestion not found")
    sug.status = "dismissed"
    db.commit()
    return {"dismissed": sug_id}


# ── AI rule-suggestions (advisory; separate from the learned-from-manual ones) ─

@router.get("/rule-suggestions-ai")
def rule_suggestions_ai(partner: str = Query(...),
                        date_from: str = Query(""), date_to: str = Query(""),
                        db: Session = Depends(get_db), user=Depends(get_current_user)):
    """
    AI-proposed matching rules for a partner, from a DE-IDENTIFIED view of its
    unmatched rows. Advisory + ephemeral (recomputed each call) — a human promotes
    one via /rule-suggestions-ai/accept. Tagged source:"ai". Never writes.
    """
    def base(side):
        q = db.query(Transaction).filter(
            Transaction.partner == partner, Transaction.side == side,
            Transaction.recon_status == ReconStatus.unmatched,
            Transaction.row_type == "txn",
        )
        if date_from: q = q.filter(Transaction.recon_date >= date_from)
        if date_to:   q = q.filter(Transaction.recon_date <= date_to)
        return q.limit(3000).all()

    from models.database import MatchRule
    existing = []
    for r in db.query(MatchRule).filter(MatchRule.partner.in_([partner, "all"])).all():
        try:
            existing.append(json.loads(r.match_fields or "[]"))
        except Exception:
            pass

    from core.ai_rules import suggest_rules
    return suggest_rules(base("bank"), base("internal"), partner, existing)


class AiRuleAccept(BaseModel):
    partner: str
    fields: List[str]
    name: Optional[str] = None


@router.post("/rule-suggestions-ai/accept")
def accept_ai_rule(body: AiRuleAccept, db: Session = Depends(get_db),
                   user=Depends(require_permission("logic_builder"))):
    """Promote an AI-proposed rule to a real MatchRule (appended at lowest priority).
    Constrained to the valid match-field vocabulary so the created rule always works."""
    from models.database import MatchRule, PartnerConfig
    from core.ai_rules import ALLOWED_FIELDS
    fields = [f for f in (body.fields or []) if f in ALLOWED_FIELDS]
    fields = list(dict.fromkeys(fields))
    if not fields or fields == ["amount"]:
        raise HTTPException(400, "A rule needs a valid field combination (not amount alone).")
    # Bind to a real, single partner (canonical slug) — never trust the client to widen scope.
    # A global "all" rule reclassifies every partner's money, so it stays admin-only (admin CRUD).
    partner = (body.partner or "").strip().lower()
    if not partner or partner == "all":
        raise HTTPException(400, "Choose a specific partner — global 'all' rules are not allowed here.")
    if not db.query(PartnerConfig).filter(PartnerConfig.slug == partner).first():
        raise HTTPException(404, f"Unknown partner '{partner}'.")
    max_prio = db.query(func.max(MatchRule.priority)).filter(MatchRule.partner == partner).scalar() or 0
    name = (body.name or "").strip() or f"AI: {' + '.join(f.replace('_', ' ').title() for f in fields)}"
    rule = MatchRule(name=name, partner=partner, priority=max_prio + 1,
                     match_fields=json.dumps(fields), is_active=True)
    db.add(rule)
    db.commit()
    _log(db, user, "ai_rule_suggestion_accepted", "match_rule", rule.id,
         detail={"partner": partner, "fields": fields, "source": "ai"})
    return {"created_rule": rule.name, "partner": partner, "priority": rule.priority}


# ═══════════════════════════════ Anomalies ═══════════════════════════════════

@router.get("/anomalies")
def anomalies(partner: str = Query(""), db: Session = Depends(get_db),
              user=Depends(get_current_user)):
    """Rule-based anomaly scan: duplicate references + daily volume spikes."""
    out = {"duplicate_utr": [], "duplicate_tid": [], "volume_spikes": []}

    base = db.query(Transaction.utr_number, func.count(Transaction.id)) \
        .filter(Transaction.side == "bank", Transaction.utr_number.isnot(None),
                Transaction.utr_number != "", Transaction.row_type == "txn")
    if partner: base = base.filter(Transaction.partner == partner)
    for (utr, cnt) in base.group_by(Transaction.utr_number).having(func.count(Transaction.id) > 1).limit(100).all():
        out["duplicate_utr"].append({"utr_number": utr, "count": cnt})

    tq = db.query(Transaction.eko_tid, Transaction.side, func.count(Transaction.id)) \
        .filter(Transaction.eko_tid.isnot(None), Transaction.eko_tid != "", Transaction.row_type == "txn")
    if partner: tq = tq.filter(Transaction.partner == partner)
    for (tid, side, cnt) in tq.group_by(Transaction.eko_tid, Transaction.side) \
                               .having(func.count(Transaction.id) > 1).limit(100).all():
        out["duplicate_tid"].append({"eko_tid": tid, "side": side, "count": cnt})

    # Volume spike: today's row count vs trailing 7-day average per partner
    vq = db.query(Transaction.partner, Transaction.recon_date, func.count(Transaction.id)) \
        .filter(Transaction.row_type == "txn", Transaction.side == "internal")
    if partner: vq = vq.filter(Transaction.partner == partner)
    daily = vq.group_by(Transaction.partner, Transaction.recon_date).all()
    series = {}
    for (p, d, c) in daily:
        if p and d and len(str(d)) == 10:
            series.setdefault(p, []).append((d, c))
    for p, pts in series.items():
        pts.sort()
        if len(pts) < 4:
            continue
        latest_date, latest = pts[-1]
        prev = [c for (_d, c) in pts[-8:-1]]
        avg = sum(prev) / len(prev) if prev else 0
        if avg > 0 and (latest > avg * 2 or latest < avg * 0.4):
            out["volume_spikes"].append({
                "partner": p, "date": latest_date, "count": latest,
                "trailing_avg": round(avg, 1),
                "direction": "spike" if latest > avg * 2 else "drop",
            })
    return out


@router.get("/anomalies-ai")
def anomalies_ai(partner: str = Query(""),
                 date_from: str = Query(""), date_to: str = Query(""),
                 db: Session = Depends(get_db), user=Depends(get_current_user)):
    """
    AI-detected anomalies the rule scan structurally can't catch (near-duplicate
    references, same-amount clusters, timing outliers). Advisory only — the model
    sees a DE-IDENTIFIED payload and never writes. Tagged source:"ai".
    """
    q = db.query(Transaction).filter(Transaction.row_type == "txn")
    if partner:   q = q.filter(Transaction.partner == partner)
    if date_from: q = q.filter(Transaction.recon_date >= date_from)
    if date_to:   q = q.filter(Transaction.recon_date <= date_to)
    # Favour the rows most worth scrutinising: unmatched + amount_mismatch first.
    q = q.filter(Transaction.recon_status.in_([
        ReconStatus.unmatched, ReconStatus.amount_mismatch, ReconStatus.src_assigned]))
    rows = q.order_by(Transaction.recon_date.desc()).limit(400).all()

    from core.ai_anomalies import detect_anomalies
    return detect_anomalies(rows)
