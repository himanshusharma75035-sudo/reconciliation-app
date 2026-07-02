"""
core/funds.py — Funds-position (EOD balance) feature. REPORTING ONLY:
nothing here is read by any matching engine.

record_snapshots(...) is the single shared writer, called at ingest time by every
bank-statement upload path (core interactive, watch-folder, SBI, E-Value). It gets
the parsed rows IN FILE ORDER (statement order is only known at ingest; row IDs are
UUIDs) and upserts one BankBalanceSnapshot per (product, partner, account,
statement_date). Never raises — a snapshot failure must never block an upload.

get_funds_position(...) answers "what were the funds at EOD of date D, as per the
data given" — latest statement per account ≤ D, with staleness + day-over-day delta.
"""
import logging
import datetime

from models.database import BankBalanceSnapshot, generate_id

logger = logging.getLogger("eko_recon.funds")

_DATEISH = tuple("0123456789")


def record_snapshots(db, product: str, partner: str, account: str, lines, uploaded_by=None):
    """Upsert per-date balance snapshots from parsed statement lines (file order).

    lines: iterable of dicts:
      date     'YYYY-MM-DD' (rows with non-date-like values are skipped)
      balance  running balance after the row (None if the format has none)
      dr, cr   money movement of the row (0 for notification lines)
      stated_opening / stated_closing  optional explicit values from
               OPENING/CLOSING BALANCE notification lines
    """
    try:
        days = {}
        for ln in lines:
            d = (ln.get("date") or "")[:10]
            if len(d) != 10 or not d.startswith(_DATEISH):
                continue
            a = days.setdefault(d, {"dr": 0.0, "cr": 0.0, "n": 0, "last_bal": None,
                                    "stated_open": None, "stated_close": None})
            if ln.get("stated_opening") is not None:
                a["stated_open"] = float(ln["stated_opening"])
            if ln.get("stated_closing") is not None:
                a["stated_close"] = float(ln["stated_closing"])
            dr, cr = float(ln.get("dr") or 0), float(ln.get("cr") or 0)
            if dr or cr:
                a["dr"] = round(a["dr"] + dr, 2)
                a["cr"] = round(a["cr"] + cr, 2)
                a["n"] += 1
            if ln.get("balance") is not None:
                a["last_bal"] = float(ln["balance"])

        wrote = 0
        for d, a in days.items():
            closing = a["stated_close"] if a["stated_close"] is not None else a["last_bal"]
            if closing is None and a["stated_open"] is None:
                continue    # no balance information at all for this day
            source = "stated" if a["stated_close"] is not None else "running"
            opening = a["stated_open"]
            if opening is None and closing is not None:
                opening = round(closing - (a["cr"] - a["dr"]), 2)   # self-consistent derive
                if source == "running":
                    source = "derived"
            row = db.query(BankBalanceSnapshot).filter(
                BankBalanceSnapshot.product == product,
                BankBalanceSnapshot.partner == partner,
                BankBalanceSnapshot.bank_account == (account or ""),
                BankBalanceSnapshot.statement_date == d).first()
            if not row:
                row = BankBalanceSnapshot(id=generate_id(), product=product, partner=partner,
                                          bank_account=(account or ""), statement_date=d)
                db.add(row)
            row.opening_balance = opening
            row.closing_balance = closing
            row.total_dr, row.total_cr = a["dr"], a["cr"]
            row.txn_count = a["n"]
            row.source = source
            row.uploaded_by = uploaded_by
            row.updated_at = datetime.datetime.utcnow()
            wrote += 1
        db.commit()
        return wrote
    except Exception:
        logger.exception("funds snapshot failed (upload unaffected)")
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def get_funds_position(db, as_of: str):
    """Latest statement per (product, partner, account) with statement_date <= as_of,
    plus day-over-day delta and staleness. Returns {rows, totals_by_product, as_of}."""
    snaps = db.query(BankBalanceSnapshot).filter(
        BankBalanceSnapshot.statement_date <= as_of
    ).order_by(BankBalanceSnapshot.statement_date.asc()).all()
    latest, prev = {}, {}
    for s in snaps:                       # ascending → last write wins as latest
        k = (s.product, s.partner, s.bank_account or "")
        if k in latest:
            prev[k] = latest[k]
        latest[k] = s
    rows = []
    for k, s in sorted(latest.items()):
        p = prev.get(k)
        rows.append({
            "product": s.product, "partner": s.partner, "bank_account": s.bank_account or "",
            "statement_date": s.statement_date,
            "stale": s.statement_date < as_of,
            "opening_balance": s.opening_balance, "closing_balance": s.closing_balance,
            "total_dr": s.total_dr, "total_cr": s.total_cr, "txn_count": s.txn_count,
            "source": s.source,
            "prev_date": p.statement_date if p else None,
            "prev_closing": p.closing_balance if p else None,
            "delta": (round((s.closing_balance or 0) - (p.closing_balance or 0), 2)
                      if p and p.closing_balance is not None and s.closing_balance is not None
                      else None),
        })
    totals = {}
    for r in rows:
        t = totals.setdefault(r["product"], {"accounts": 0, "closing_total": 0.0, "stale": 0})
        t["accounts"] += 1
        t["closing_total"] = round(t["closing_total"] + (r["closing_balance"] or 0), 2)
        t["stale"] += 1 if r["stale"] else 0
    return {"as_of": as_of, "rows": rows, "totals_by_product": totals}
