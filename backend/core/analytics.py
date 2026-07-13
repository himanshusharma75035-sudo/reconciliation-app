"""
core/analytics.py — Executive reconciliation analytics. REPORTING ONLY:
nothing here is read by any matching engine; it aggregates existing result rows.

build_analytics(db, date_from, date_to, product, side) returns a single payload the
executive dashboard renders as KPI cards + charts:
  totals      — headline numbers for the range (matched / unmatched / rate / volume)
  by_product  — one row per product (core partners + evalue + sbi_kiosk + bbps)
  by_status   — matched / unmatched / mismatch / other (for the status pie)
  by_side     — bank vs internal matched/unmatched for genuine two-sided ledgers
  daily       — per-date matched / unmatched / total / rate (for the trend chart)

Every product's own status vocabulary is folded into four buckets so the CEO sees
one consistent picture across core ledger, E-Value, SBI Kiosk and BBPS.
"""

# ── Status → bucket mapping (lower-cased match) ───────────────────────────────
_MATCHED = {
    "matched", "manual_matched", "interbank_matched",          # core ledger
    "matched_online", "matched_cash", "matched_manual",        # e-value
    "sbi_matched",                                             # (label form)
}
_UNMATCHED = {
    "unmatched", "unmatched_bank", "unmatched_load",
    "unmatched_txnreport",
}
_MISMATCH = {"amount_mismatch", "wrong_amount"}
# everything else (src_assigned, duplicate, failed, bank_debit, transaction_fee,
# fund_transfer, fee_matched, reversal, …) → "other"


def _bucket(status: str) -> str:
    s = (status or "").strip().lower()
    if s in _MATCHED:
        return "matched"
    if s in _UNMATCHED:
        return "unmatched"
    if s in _MISMATCH:
        return "mismatch"
    return "other"


def _blank():
    return {"matched": 0, "unmatched": 0, "mismatch": 0, "other": 0,
            "matched_volume": 0.0, "open_volume": 0.0}


def _pretty(product: str) -> str:
    return {"evalue": "E-Value", "sbi_kiosk": "SBI Kiosk", "bbps": "BBPS",
            "qr": "QR Collection", "pg": "Accept Payment (PG)", "aeps": "AePS Cashout",
            "dmt": "DMT", "indonepal": "Indo-Nepal", "digikhata": "Digikhata (PPI)"}.get(
        product, product.replace("_", " ").title())


def build_analytics(db, date_from=None, date_to=None, product=None, side=None):
    from sqlalchemy import func as F
    from models.database import (Transaction, EvalueBankTxn, EvalueWalletLoad,
                                 SBIP02Result)
    try:
        from models.database import BbpsBankTxn, BbpsInternal
    except Exception:
        BbpsBankTxn = BbpsInternal = None

    # records: list of (product, side, date, status, count, matched_amt, open_amt)
    # kept coarse (GROUP BY) so even the 100k-row SBI table stays cheap.
    recs = []

    def _in_range(d):
        if not d or len(d) < 10 or not d[:4].isdigit():
            return False
        if date_from and d < date_from:
            return False
        if date_to and d > date_to:
            return False
        return True

    # ── 1. Core ledger (all core-ledger partners), txn rows only ──────────────
    q = db.query(Transaction.partner, Transaction.side, Transaction.recon_date,
                 Transaction.recon_status, F.count(Transaction.id), F.sum(Transaction.amount)) \
           .filter(Transaction.row_type == "txn",
                   Transaction.recon_date.isnot(None),
                   Transaction.recon_date.like("20%"))
    if date_from:
        q = q.filter(Transaction.recon_date >= date_from)
    if date_to:
        q = q.filter(Transaction.recon_date <= date_to)
    for partner, sd, d, status, cnt, amt in q.group_by(
            Transaction.partner, Transaction.side, Transaction.recon_date,
            Transaction.recon_status).all():
        if not partner or not _in_range(d):
            continue
        recs.append((partner, sd or "bank", d, _statv(status), cnt or 0, float(amt or 0)))

    # ── 2. E-Value (bank + wallet-load sides, own tables) ─────────────────────
    for model, datecol, sd in ((EvalueBankTxn, EvalueBankTxn.txn_date, "bank"),
                               (EvalueWalletLoad, EvalueWalletLoad.transaction_date, "internal")):
        eq = db.query(datecol, model.recon_status, F.count(model.id), F.sum(model.amount))
        if date_from:
            eq = eq.filter(datecol >= date_from)
        if date_to:
            eq = eq.filter(datecol <= date_to)
        for d, status, cnt, amt in eq.group_by(datecol, model.recon_status).all():
            if not _in_range((d or "")[:10]):
                continue
            recs.append(("evalue", sd, d[:10], _statv(status), cnt or 0, float(amt or 0)))

    # ── 3. SBI Kiosk — P02 (bank statement ↔ transaction report) main matching ─
    sq = db.query(SBIP02Result.recon_date, SBIP02Result.match_status,
                  F.count(SBIP02Result.id), F.sum(SBIP02Result.bank_amount))
    if date_from:
        sq = sq.filter(SBIP02Result.recon_date >= date_from)
    if date_to:
        sq = sq.filter(SBIP02Result.recon_date <= date_to)
    for d, status, cnt, amt in sq.group_by(SBIP02Result.recon_date, SBIP02Result.match_status).all():
        if not _in_range((d or "")[:10]):
            continue
        # P02 result rows pair bank↔report; treat as the kiosk product (no side split).
        recs.append(("sbi_kiosk", "bank", d[:10], status, cnt or 0, float(amt or 0)))

    # ── 4. BBPS (if populated) ────────────────────────────────────────────────
    if BbpsBankTxn is not None:
        for model, sd in ((BbpsBankTxn, "bank"), (BbpsInternal, "internal")):
            try:
                bq = db.query(model.txn_date, model.recon_status, F.count(model.id), F.sum(model.amount))
                if date_from:
                    bq = bq.filter(model.txn_date >= date_from)
                if date_to:
                    bq = bq.filter(model.txn_date <= date_to)
                for d, status, cnt, amt in bq.group_by(model.txn_date, model.recon_status).all():
                    if not _in_range((d or "")[:10]):
                        continue
                    recs.append(("bbps", sd, d[:10], _statv(status), cnt or 0, float(amt or 0)))
            except Exception:
                pass

    # ── Optional filters ──────────────────────────────────────────────────────
    if product:
        recs = [r for r in recs if r[0] == product]
    if side in ("bank", "internal"):
        recs = [r for r in recs if r[1] == side]

    # ── Roll up ───────────────────────────────────────────────────────────────
    by_product, by_side_map, daily_map, products = {}, {"bank": _blank(), "internal": _blank()}, {}, set()
    totals = _blank()
    for prod, sd, d, status, cnt, amt in recs:
        products.add(prod)
        b = _bucket(status)
        for bag in (totals, by_product.setdefault(prod, _blank()),
                    by_side_map.setdefault(sd, _blank()), daily_map.setdefault(d, _blank())):
            bag[b] += cnt
            if b == "matched":
                bag["matched_volume"] = round(bag["matched_volume"] + amt, 2)
            elif b == "unmatched":
                bag["open_volume"] = round(bag["open_volume"] + amt, 2)

    def _rate(bag):
        m = bag["matched"]; denom = m + bag["unmatched"] + bag["mismatch"]
        return round(m / denom * 100, 1) if denom else 0.0

    def _total(bag):
        return bag["matched"] + bag["unmatched"] + bag["mismatch"] + bag["other"]

    totals["transactions"] = _total(totals)
    totals["match_rate"] = _rate(totals)

    by_product_list = sorted(
        ({"product": p, "label": _pretty(p), **v, "transactions": _total(v), "match_rate": _rate(v)}
         for p, v in by_product.items()),
        key=lambda x: -x["transactions"])

    by_side_list = [{"side": s, **by_side_map[s], "transactions": _total(by_side_map[s]),
                     "match_rate": _rate(by_side_map[s])}
                    for s in ("bank", "internal") if _total(by_side_map[s]) > 0]

    by_status_list = [{"status": k, "label": k.title(), "count": totals[k]}
                      for k in ("matched", "unmatched", "mismatch", "other") if totals[k] > 0]

    daily_list = [{"date": d, **daily_map[d], "transactions": _total(daily_map[d]),
                   "match_rate": _rate(daily_map[d])}
                  for d in sorted(daily_map)]

    return {
        "date_from": date_from, "date_to": date_to,
        "product": product, "side": side,
        "totals": totals,
        "by_product": by_product_list,
        "by_status": by_status_list,
        "by_side": by_side_list,
        "daily": daily_list,
        "products": [{"product": p, "label": _pretty(p)} for p in sorted(products, key=_pretty)],
    }


def _statv(status) -> str:
    """Enum-or-str → plain string (MySQL returns str, SQLite may return the enum)."""
    return str(getattr(status, "value", status) or "")
