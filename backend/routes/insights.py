"""
routes/insights.py

Cross-cutting data-quality and reporting features:
  • Pre-recon sanity check (bank vs internal row counts)
  • Configurable D+N settlement carry-forward matching
  • Monthly trend dashboard (match rate + ageing)
  • D+7 ageing escalation list + raise-with-partner email draft
  • Monthly reconciliation certificate (PDF)
"""
import io
import json
import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional

from models.database import get_db, Transaction, ReconStatus, PartnerConfig, AuditLog, generate_id
from core.auth import get_current_user, require_permission

logger = logging.getLogger("eko_recon")
router = APIRouter(prefix="/api/insights", tags=["insights"])

_OPEN = [ReconStatus.unmatched, ReconStatus.src_assigned, ReconStatus.amount_mismatch]
_MATCHED = [ReconStatus.matched, ReconStatus.manual_matched, ReconStatus.interbank_matched]


def _norm_utr(s):
    import re
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


# ── 1) Pre-recon sanity check ─────────────────────────────────────────────────
@router.get("/pre-recon-check")
def pre_recon_check(partner: str = Query(...), recon_date: str = Query(...),
                    db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Row-count comparison before running matching, to catch a missing file."""
    def _n(side):
        return db.query(func.count(Transaction.id)).filter(
            Transaction.partner == partner, Transaction.side == side,
            Transaction.recon_date == recon_date, Transaction.row_type == "txn").scalar() or 0
    bank, internal = _n("bank"), _n("internal")
    diff = bank - internal
    warn = None
    if bank == 0 or internal == 0:
        warn = "One side has no data — a file may be missing."
    elif abs(diff) > max(10, 0.1 * max(bank, internal)):
        warn = "Large count difference — verify both files are correct and complete."
    return {"partner": partner, "recon_date": recon_date, "bank": bank, "internal": internal,
            "difference": diff, "warning": warn, "proceed_ok": warn is None}


# ── 2) D+N settlement carry-forward ───────────────────────────────────────────
@router.post("/run-carry-forward")
def run_carry_forward(partner: str = Query(...), recon_date: str = Query(...),
                      db: Session = Depends(get_db),
                      user=Depends(require_permission("run_recon"))):
    """
    Generalised NEFT D+1 → D+N. Matches unmatched bank rows from the previous
    1..carry_days days (that carry a UTR) against today's unmatched internal rows
    on UTR. carry_days comes from the partner config (1 = D+1, 2 = T+2, ...).
    """
    cfg = db.query(PartnerConfig).filter(PartnerConfig.slug == partner).first()
    carry_days = (cfg.settlement_carry_days if cfg and cfg.settlement_carry_days is not None else 1)
    try:
        d0 = datetime.date.fromisoformat(recon_date)
    except Exception:
        raise HTTPException(400, "recon_date must be YYYY-MM-DD")

    internal = db.query(Transaction).filter(
        Transaction.partner == partner, Transaction.side == "internal",
        Transaction.recon_date == recon_date, Transaction.recon_status == ReconStatus.unmatched,
        Transaction.row_type == "txn").all()
    int_by_utr = {}
    for t in internal:
        u = _norm_utr(t.utr_number)
        if u:
            int_by_utr.setdefault(u, []).append(t)

    matched = 0
    seq = db.query(func.count(func.distinct(Transaction.match_id))).filter(
        Transaction.match_id.like(f"CFW-{partner.upper()}-{recon_date.replace('-','')}-%")).scalar() or 0
    for back in range(1, carry_days + 1):
        d = (d0 - datetime.timedelta(days=back)).isoformat()
        bank_rows = db.query(Transaction).filter(
            Transaction.partner == partner, Transaction.side == "bank",
            Transaction.recon_date == d, Transaction.recon_status == ReconStatus.unmatched,
            Transaction.row_type == "txn").all()
        for b in bank_rows:
            u = _norm_utr(b.utr_number)
            cands = int_by_utr.get(u)
            if not u or not cands:
                continue
            i = cands.pop(0)
            seq += 1
            mid = f"CFW-{partner.upper()}-{recon_date.replace('-','')}-{seq:04d}"
            for row, other in ((b, i), (i, b)):
                row.recon_status = ReconStatus.matched
                row.match_id = mid
                row.matched_with_id = other.id
            b.src_note = f"carry-forward D+{back}"
            matched += 1
    db.add(AuditLog(id=generate_id(), user_id=getattr(user, "id", None),
                    username=getattr(user, "username", None), action="run_carry_forward",
                    action_type="human", entity_type="recon",
                    detail=json.dumps({"partner": partner, "recon_date": recon_date,
                                       "carry_days": carry_days, "matched": matched})))
    db.commit()
    return {"partner": partner, "recon_date": recon_date, "carry_days": carry_days, "matched": matched}


# ── 3) Monthly trend dashboard ────────────────────────────────────────────────
@router.get("/trend")
def trend(partner: Optional[str] = None, months: int = Query(3, ge=1, le=12),
          db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Per-PRODUCT match rate + open count for the last `months` calendar months, across
    EVERY product (core ledger + E-Value + BBPS + SBI Kiosk + AePS/QR)."""
    today = datetime.date.today()
    periods = []
    y, m = today.year, today.month
    for _ in range(months):
        periods.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12; y -= 1
    periods = list(reversed(periods))

    # Per-PRODUCT trend across ALL products (core ledger + E-Value + BBPS + SBI Kiosk +
    # AePS/QR) via the same aggregator the analytics dashboard uses — so Insights covers
    # every product, not just the core Transaction table. Called once per month.
    from core.analytics import build_analytics
    # The dropdown sends a core partner slug; map it to its product GROUP (build_analytics
    # filters by group). None => all products.
    prod_filter = None
    if partner:
        cfg = db.query(PartnerConfig).filter(PartnerConfig.slug == partner).first()
        prod_filter = (cfg.product if cfg and cfg.product else partner)

    series = {}   # product label -> { ym -> {total, matched, open, match_rate} }
    for ym in periods:
        a = build_analytics(db, f"{ym}-01", f"{ym}-31", product=prod_filter)
        for g in a.get("by_group", []):
            label = g.get("label") or g.get("group") or "—"
            series.setdefault(label, {})[ym] = {
                "total":      g.get("transactions", 0),
                "matched":    g.get("matched", 0),
                "open":       g.get("unmatched", 0) + g.get("mismatch", 0),
                "match_rate": g.get("match_rate", 0),
            }
    return {"periods": periods,
            "partners": [{"partner": label,
                          "by_month": [series[label].get(ym, {"total": 0, "matched": 0, "open": 0, "match_rate": 0})
                                       for ym in periods]}
                         for label in sorted(series)]}


# ── 4) Ageing escalation + raise-with-partner email draft ─────────────────────
@router.get("/escalation")
def escalation(partner: Optional[str] = None, db: Session = Depends(get_db),
               user=Depends(get_current_user)):
    """Bank-side open items aged beyond D+7 (escalation list)."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    q = db.query(Transaction).filter(
        Transaction.side == "bank", Transaction.row_type == "txn",
        Transaction.recon_status.in_(_OPEN), Transaction.recon_date <= cutoff)
    if partner:
        q = q.filter(Transaction.partner == partner)
    rows = q.order_by(Transaction.recon_date).all()
    items = []
    for t in rows:
        try:
            age = (datetime.date.today() - datetime.date.fromisoformat(t.recon_date)).days
        except Exception:
            age = None
        items.append({"id": t.id, "partner": t.partner, "recon_date": t.recon_date, "age_days": age,
                      "eko_tid": t.eko_tid, "tracking_number": t.tracking_number, "utr": t.utr_number,
                      "amount": t.amount})
    total_amt = round(sum(i["amount"] or 0 for i in items), 2)
    return {"count": len(items), "total_amount": total_amt, "items": items}


@router.get("/escalation/email-draft")
def escalation_email(partner: str = Query(...), db: Session = Depends(get_db),
                     user=Depends(get_current_user)):
    """Draft an email to raise the D+7 open items with the partner bank."""
    esc = escalation(partner=partner, db=db, user=user)
    cfg = db.query(PartnerConfig).filter(PartnerConfig.slug == partner).first()
    pname = cfg.display_name if cfg else partner
    lines = "\n".join(
        f"  {i+1}. {it['recon_date']} | TID {it['eko_tid'] or '-'} | UTR {it['utr'] or '-'} | "
        f"Tracking {it['tracking_number'] or '-'} | ₹{it['amount']}"
        for i, it in enumerate(esc["items"][:50]))
    subject = f"[EKO Recon] {pname} — {esc['count']} unreconciled transactions aged D+7+ (₹{esc['total_amount']})"
    body = (
        f"Dear {pname} Team,\n\n"
        f"The following {esc['count']} transaction(s), totalling ₹{esc['total_amount']}, remain "
        f"unreconciled in our records beyond 7 days and need your confirmation of settlement / "
        f"credit status:\n\n{lines}\n\n"
        f"Kindly confirm the status of these at the earliest so we can close them.\n\n"
        f"Regards,\nFinance Operations\nEKO Bharat Ventures Pvt. Ltd.")
    return {"partner": partner, "subject": subject, "body": body, "count": esc["count"]}


# ── 5) Monthly reconciliation certificate (PDF) ───────────────────────────────
@router.get("/certificate")
def certificate(partner: str = Query(...), month: str = Query(..., description="YYYY-MM"),
                db: Session = Depends(get_db),
                user=Depends(require_permission("reports"))):
    """One formal PDF per partner per month — for manager sign-off and filing."""
    try:
        y, m = month.split("-"); start = f"{y}-{m}-01"
        last_day = (datetime.date(int(y), int(m) % 12 + 1, 1) - datetime.timedelta(days=1)) \
            if int(m) != 12 else datetime.date(int(y), 12, 31)
        end = last_day.isoformat()
    except Exception:
        raise HTTPException(400, "month must be YYYY-MM")

    cfg = db.query(PartnerConfig).filter(PartnerConfig.slug == partner).first()
    pname = cfg.display_name if cfg else partner

    def _q(side):
        return db.query(Transaction).filter(
            Transaction.partner == partner, Transaction.side == side,
            Transaction.recon_date >= start, Transaction.recon_date <= end,
            Transaction.row_type == "txn")
    bank = _q("bank").all(); internal = _q("internal").all()

    def _sum(rows):
        return round(sum(t.amount or 0 for t in rows), 2)
    matched = [t for t in bank if t.recon_status in _MATCHED]
    unmatched = [t for t in bank if t.recon_status in _OPEN]
    bank_total = _sum(bank); internal_total = _sum(internal)
    stats = {
        "Bank transactions": (len(bank), bank_total),
        "Internal transactions": (len(internal), internal_total),
        "Matched": (len(matched), _sum(matched)),
        "Unmatched / open (bank)": (len(unmatched), _sum(unmatched)),
    }
    match_rate = round(len(matched) / (len(bank) or 1) * 100, 1)

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle)
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    except Exception:
        raise HTTPException(500, "PDF generation requires reportlab. Run: pip install reportlab")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    ss = getSampleStyleSheet()
    navy = colors.HexColor("#1F4E79")
    h = ParagraphStyle("h", parent=ss["Title"], textColor=navy, fontSize=18)
    sub = ParagraphStyle("sub", parent=ss["Normal"], fontSize=11, textColor=colors.HexColor("#444444"))
    el = []
    el.append(Paragraph("EKO Bharat Ventures Pvt. Ltd.", ParagraphStyle("c", parent=ss["Normal"], fontSize=12, textColor=navy)))
    el.append(Paragraph("Monthly Reconciliation Certificate", h))
    el.append(Paragraph(f"Partner: <b>{pname}</b> &nbsp;&nbsp; Period: <b>{month}</b> ({start} to {end})", sub))
    el.append(Spacer(1, 8 * mm))
    data = [["Particulars", "Count", "Amount (₹)"]]
    for k, (c, a) in stats.items():
        data.append([k, f"{c:,}", f"{a:,.2f}"])
    data.append(["Overall match rate", f"{match_rate}%", ""])
    tbl = Table(data, colWidths=[90 * mm, 35 * mm, 45 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), navy),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EAF1FA")]),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
    ]))
    el.append(tbl)
    el.append(Spacer(1, 12 * mm))
    el.append(Paragraph(
        f"This certifies that the {pname} reconciliation for {month} has been performed on the "
        f"EKO Reconciliation platform, with {len(matched):,} of {len(bank):,} bank transactions "
        f"matched ({match_rate}%) and {len(unmatched):,} item(s) open at the time of issue.", sub))
    el.append(Spacer(1, 18 * mm))
    sign = Table([["Prepared by", "Reviewed / Approved by"],
                  ["_____________________", "_____________________"],
                  ["Finance Operations", "Finance Manager"]], colWidths=[85 * mm, 85 * mm])
    sign.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 1), (-1, 1), 18)]))
    el.append(sign)
    el.append(Spacer(1, 10 * mm))
    el.append(Paragraph(
        f"Generated {datetime.datetime.now().strftime('%d %b %Y %H:%M IST')} · Confidential — Internal Use Only",
        ParagraphStyle("f", parent=ss["Normal"], fontSize=8, textColor=colors.HexColor("#999999"))))
    doc.build(el)
    buf.seek(0)
    fn = f"recon_certificate_{partner}_{month}.pdf"
    return StreamingResponse(buf, media_type="application/pdf",
                             headers={"Content-Disposition": f'attachment; filename="{fn}"'})
