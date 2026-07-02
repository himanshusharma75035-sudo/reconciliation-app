"""
core/funds.py — Funds-position (EOD balance) feature. REPORTING ONLY:
nothing here is read by any matching engine.

record_snapshots(...) is the single shared writer, called at ingest time by every
bank-statement upload path (core interactive, watch-folder, SBI, E-Value). It gets
the parsed rows IN FILE ORDER (statement order is only known at ingest; row IDs are
UUIDs) and upserts one BankBalanceSnapshot per (product, partner, account,
statement_date). Never raises — a snapshot failure must never block an upload.

get_funds_position(...) answers "what were the funds at EOD of date D, as per the
data given" — exactly the statements DATED D (pick another date to see that day),
with day-over-day delta vs each account's previous available statement.

For statement formats with NO running-balance column (QR / AePS / Indo-Nepal
transaction reports), balances are chained: someone enters an opening balance for
one day via set_opening_anchor(...), and every later day's closing is computed as
previous closing + CR − DR from the stored statement rows — record_snapshots keeps
extending the chain automatically on each new upload.
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
        # Ascending so balance-less days can chain off earlier days in this upload.
        ordered = sorted(days)
        # Chain seed: the account's latest known closing before this upload's first day.
        chain = None
        if ordered:
            prev = db.query(BankBalanceSnapshot).filter(
                BankBalanceSnapshot.product == product,
                BankBalanceSnapshot.partner == partner,
                BankBalanceSnapshot.bank_account == (account or ""),
                BankBalanceSnapshot.statement_date < ordered[0],
                BankBalanceSnapshot.closing_balance.isnot(None),
            ).order_by(BankBalanceSnapshot.statement_date.desc()).first()
            chain = prev.closing_balance if prev else None
        for d in ordered:
            a = days[d]
            row = db.query(BankBalanceSnapshot).filter(
                BankBalanceSnapshot.product == product,
                BankBalanceSnapshot.partner == partner,
                BankBalanceSnapshot.bank_account == (account or ""),
                BankBalanceSnapshot.statement_date == d).first()
            closing = a["stated_close"] if a["stated_close"] is not None else a["last_bal"]
            opening = a["stated_open"]
            if closing is not None:
                source = "stated" if a["stated_close"] is not None else "running"
                if opening is None:
                    opening = round(closing - (a["cr"] - a["dr"]), 2)   # self-consistent derive
                    if source == "running":
                        source = "derived"
            elif row is not None and row.source == "manual" and row.opening_balance is not None:
                # User-anchored opening for this exact day: recompute its closing.
                opening = row.opening_balance
                closing = round(opening + a["cr"] - a["dr"], 2)
                source = "manual"
            elif chain is not None:
                # No balance column (QR/AePS/Indo-Nepal): chain from previous closing.
                opening = chain
                closing = round(chain + a["cr"] - a["dr"], 2)
                source = "running"
            elif opening is not None:
                closing = round(opening + a["cr"] - a["dr"], 2)
                source = "derived"
            else:
                continue    # no balance information and no chain anchor yet
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
            chain = closing
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
    """Statements dated EXACTLY as_of, one row per (product, partner, account), with
    day-over-day delta vs the account's previous available statement. To see another
    day, ask for that date. Returns {rows, totals_by_product, as_of}."""
    snaps = db.query(BankBalanceSnapshot).filter(
        BankBalanceSnapshot.statement_date == as_of).all()
    # Previous available statement per account (for the delta column).
    prev = {}
    if snaps:
        older = db.query(BankBalanceSnapshot).filter(
            BankBalanceSnapshot.statement_date < as_of
        ).order_by(BankBalanceSnapshot.statement_date.asc()).all()
        for s in older:                   # ascending → last write wins as most recent
            prev[(s.product, s.partner, s.bank_account or "")] = s
    rows = []
    for s in sorted(snaps, key=lambda s: (s.product, s.partner, s.bank_account or "")):
        p = prev.get((s.product, s.partner, s.bank_account or ""))
        rows.append({
            "product": s.product, "partner": s.partner, "bank_account": s.bank_account or "",
            "statement_date": s.statement_date,
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
        t = totals.setdefault(r["product"], {"accounts": 0, "closing_total": 0.0})
        t["accounts"] += 1
        t["closing_total"] = round(t["closing_total"] + (r["closing_balance"] or 0), 2)
    return {"as_of": as_of, "rows": rows, "totals_by_product": totals}


def _day_aggregates(db, product: str, partner: str, account: str, from_date: str):
    """{date: {dr, cr, n}} from the product's stored statement rows, dates >= from_date.
    Reads the same tables the statements were ingested into — bank side only."""
    days = {}

    def add(d, dr, cr):
        if not d or len(d) < 10 or not d[:1].isdigit():
            return
        a = days.setdefault(d[:10], {"dr": 0.0, "cr": 0.0, "n": 0})
        a["dr"] = round(a["dr"] + dr, 2)
        a["cr"] = round(a["cr"] + cr, 2)
        a["n"] += 1 if (dr or cr) else 0

    if product == "kiosk":
        from models.database import SBIBankTransaction
        for r in db.query(SBIBankTransaction).filter(
                SBIBankTransaction.txn_date >= from_date).yield_per(2000):
            add(r.txn_date, float(r.debit or 0), float(r.credit or 0))
    elif product == "evalue":
        from models.database import EvalueBankTxn
        for r in db.query(EvalueBankTxn).filter(
                EvalueBankTxn.reco_acc_no == account,
                EvalueBankTxn.txn_date >= from_date).yield_per(2000):
            amt = float(r.amount or 0)
            if (r.dr_cr or "").upper() == "DR":
                add(r.txn_date, amt, 0.0)
            elif (r.dr_cr or "").upper() == "CR":
                add(r.txn_date, 0.0, amt)
    else:
        # Core ledger (DMT / QR / AePS / Indo-Nepal / …) — same DR/CR split the
        # ingest hook uses. Notification rows carry no money and are skipped.
        from models.database import Transaction
        for t in db.query(Transaction).filter(
                Transaction.side == "bank",
                Transaction.partner == partner,
                Transaction.recon_date >= from_date,
                Transaction.recon_date.like("20%")).yield_per(2000):
            if (t.bank_account or "") != (account or ""):
                continue
            amt = t.amount or 0
            if not amt or t.row_type == "notification":
                continue
            if t.dr_cr == "DR":
                add(t.recon_date, amt, 0.0)
            elif t.dr_cr == "CR":
                add(t.recon_date, 0.0, amt)
            elif t.row_type in ("settlement_credit", "fund_transfer", "reversal"):
                add(t.recon_date, 0.0, amt)
            else:
                add(t.recon_date, amt, 0.0)
    return days


def set_opening_anchor(db, product: str, partner: str, account: str, date: str,
                       opening: float, uploaded_by=None):
    """User-entered opening balance for one day; computes that day's closing from the
    stored statement rows and rolls the chain forward through every later day that
    has data. Bank-stated snapshots are never overwritten — the chain adopts them.
    Returns {days_written, last_date, last_closing}."""
    days = _day_aggregates(db, product, partner, account, date)
    days.setdefault(date, {"dr": 0.0, "cr": 0.0, "n": 0})   # anchor day even if no txns
    chain, wrote, last_d = None, 0, None
    for d in sorted(days):
        a = days[d]
        row = db.query(BankBalanceSnapshot).filter(
            BankBalanceSnapshot.product == product,
            BankBalanceSnapshot.partner == partner,
            BankBalanceSnapshot.bank_account == (account or ""),
            BankBalanceSnapshot.statement_date == d).first()
        if d == date:
            day_open, source = float(opening), "manual"
        elif row is not None and row.source == "stated":
            chain, last_d = row.closing_balance, d   # bank's own figure wins; chain adopts it
            continue
        else:
            day_open, source = chain, "running"
        closing = round(day_open + a["cr"] - a["dr"], 2)
        if not row:
            row = BankBalanceSnapshot(id=generate_id(), product=product, partner=partner,
                                      bank_account=(account or ""), statement_date=d)
            db.add(row)
        row.opening_balance = day_open
        row.closing_balance = closing
        row.total_dr, row.total_cr, row.txn_count = a["dr"], a["cr"], a["n"]
        row.source = source
        row.uploaded_by = uploaded_by
        row.updated_at = datetime.datetime.utcnow()
        chain, last_d = closing, d
        wrote += 1
    db.commit()
    return {"days_written": wrote, "last_date": last_d, "last_closing": chain}


def list_bank_accounts(db):
    """Every bank account the system knows of (from snapshots + stored bank rows),
    with its latest snapshot date — feeds the opening-balance anchor form."""
    from models.database import Transaction, PartnerConfig, EvalueBankTxn, SBIBankTransaction
    accounts = {}   # (product, partner, account) → latest snapshot date or None

    for s in db.query(BankBalanceSnapshot).all():
        k = (s.product, s.partner, s.bank_account or "")
        if accounts.get(k) is None or (s.statement_date or "") > (accounts[k] or ""):
            accounts[k] = s.statement_date

    prods = {p.slug: (p.product or "dmt") for p in db.query(PartnerConfig).all()}
    for partner, acct in db.query(Transaction.partner, Transaction.bank_account).filter(
            Transaction.side == "bank").distinct():
        accounts.setdefault((prods.get(partner, "dmt"), partner, acct or ""), None)
    for bank, acct in db.query(EvalueBankTxn.bank_name, EvalueBankTxn.reco_acc_no).distinct():
        accounts.setdefault(("evalue", (bank or "evalue").lower(), acct or ""), None)
    if db.query(SBIBankTransaction.id).first():
        accounts.setdefault(("kiosk", "sbi", "SBI settlement a/c"), None)

    return [{"product": p, "partner": pa, "bank_account": a, "last_statement_date": d}
            for (p, pa, a), d in sorted(accounts.items())]
