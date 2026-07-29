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
import logging

logger = logging.getLogger("eko_recon.analytics")

# ── Status → bucket mapping (lower-cased match) ───────────────────────────────
_MATCHED = {
    "matched", "manual_matched", "interbank_matched",          # core ledger
    "internal_matched",                                        # core: internal self-match (NEFT D+1 / internal pass) — a real match
    "matched_online", "matched_cash", "matched_manual",        # e-value
    "sbi_matched",                                             # (label form)
    "reversal",                                               # SBI P02: a DR+CR pair (same ref) cancels out — reconciled, never a separate bucket
    "failed_refunded",                                        # bbps: Failed+Refunded pair IS reconciled (bbps_engine.RECON_OK)
}
_UNMATCHED = {
    "unmatched", "unmatched_bank", "unmatched_load",
    "unmatched_txnreport", "unmatched_internal",               # bbps internal leg
}
# 'partial' = SBI P02 amount-differs-beyond-tolerance (the kiosk equivalent of the
# core-ledger 'amount_mismatch') — must count as a mismatch, not fall through to other.
_MISMATCH = {"amount_mismatch", "wrong_amount", "partial"}
# everything else (src_assigned, duplicate, failed, bank_debit, transaction_fee,
# fund_transfer, fee_matched, …) → "other"


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


# Friendly label for a raw recon_status (used to explain the 'other' bucket, e.g.
# duplicate / failed / bank_debit / transaction_fee / reversal — rows that don't
# require reconciliation and so aren't matched OR open).
_OTHER_LABELS = {
    "duplicate": "Duplicate", "failed": "Failed", "0": "Failed",
    "reversal_matched": "Reversal (matched)", "reversal": "Reversal",
    "bank_debit": "Bank debit", "transaction_fee": "Fee", "fee_matched": "Fee (matched)",
    "fund_transfer": "Fund transfer", "settlement_credit": "Settlement credit",
    "src_assigned": "Source-assigned", "twice_credit": "Twice credit",
    "under_review": "Under review", "ignore": "Ignored",
}


def _status_label(s):
    key = str(s or "").strip().lower()
    return _OTHER_LABELS.get(key, (str(s or "").replace("_", " ").strip().title() or "Other"))


# Product-group display names (the PartnerConfig.product value → friendly label).
# The DMT product spans several banks (Axis / Fino / Levin / Airtel); every other
# product is a single stream. This lets the dashboard show "Axis Bank" under "DMT"
# instead of a bare "axis" that reads like its own product.
_GROUP_LABEL = {
    "dmt": "DMT", "aeps": "AePS Cashout", "qr": "QR Collection",
    "pg": "Accept Payment (PG)", "digikhata": "Digikhata (PPI)",
    "indonepal": "Indo-Nepal", "kiosk": "SBI Kiosk", "evalue": "E-Value", "bbps": "BBPS",
}


def _pretty(product: str) -> str:
    return {"evalue": "E-Value", "sbi_kiosk": "SBI Kiosk", "bbps": "BBPS",
            "qr": "QR Collection", "pg": "Accept Payment (PG)", "aeps": "AePS Cashout",
            "dmt": "DMT", "indonepal": "Indo-Nepal", "digikhata": "Digikhata (PPI)"}.get(
        product, product.replace("_", " ").title())


def _kiosk_processes(db, date_from=None, date_to=None):
    """Per-process summary for the 4 SBI Kiosk reconciliations (P01–P04), for the expandable
    breakdown under SBI Kiosk on the analytics dashboard.

    ADDITIVE and isolated: it is NOT folded into `totals`/`by_group` (kiosk there stays P02,
    the primary bank↔txn recon) — so the exec + main dashboards stay in sync and no other
    product/process is affected. Read-only over the P0x result tables."""
    from sqlalchemy import func as F
    from models.database import SBIP01Result, SBIP02Result, SBIP03Result, SBIP04Result
    # (label, model, status column, amount column, status→bucket map)
    specs = [
        ("P01 · Settlement",     SBIP01Result, SBIP01Result.status,         SBIP01Result.bank_settled,
         # two-state since 2026-07-27; legacy values folded for old rows
         {"matched": "matched", "unmatched": "unmatched",
          "credited": "matched", "pending": "unmatched", "partial": "unmatched", "excess": "unmatched"}),
        ("P02 · Bank ↔ Txn",     SBIP02Result, SBIP02Result.match_status,    SBIP02Result.bank_amount,
         # a reversal is a net-zero DR+CR pair → reconciled, counted as matched (Kiosk decision)
         {"matched": "matched", "unmatched": "unmatched", "partial": "mismatch", "reversal": "matched"}),
        ("P03 · Money out ↔ in", SBIP03Result, SBIP03Result.match_status,    SBIP03Result.txn_amount,
         {"matched": "matched", "unmatched_txnreport": "unmatched", "unmatched_bank": "unmatched"}),
        ("P04 · Wallet balance", SBIP04Result, SBIP04Result.action_required, SBIP04Result.action_amount,
         {"none": "matched", "deposit": "other", "withdrawal": "other"}),
    ]
    out = []
    for label, model, statuscol, amtcol, bmap in specs:
        q = db.query(statuscol, F.count(model.id), F.sum(amtcol))
        if date_from:
            q = q.filter(model.recon_date >= date_from)
        if date_to:
            q = q.filter(model.recon_date <= date_to)
        v = _blank()
        other_st = {}
        for st, n, amt in q.group_by(statuscol).all():
            b = bmap.get(str(st or "").strip().lower(), "other")
            v[b] += (n or 0)
            av = float(amt or 0)
            if b == "matched":
                v["matched_volume"] = round(v["matched_volume"] + av, 2)
            elif b == "unmatched":
                v["open_volume"] = round(v["open_volume"] + av, 2)
            if b == "other":
                lbl = _status_label(st)
                other_st[lbl] = other_st.get(lbl, 0) + (n or 0)
        denom = v["matched"] + v["unmatched"] + v["mismatch"]
        out.append({"process": label, **v,
                    "transactions": v["matched"] + v["unmatched"] + v["mismatch"] + v["other"],
                    "match_rate": round(v["matched"] / denom * 100, 1) if denom else 0.0,
                    "other_statuses": other_st})
    return out


def _open_ageing(db, date_from=None, date_to=None):
    """Ageing of OPEN (unmatched) items by how long they've been open — count + ₹ value per
    bucket (Current / 1–2d / 3–6d / 7+ days), across the core ledger + E-Value + SBI Kiosk.
    Answers 'how much money is stuck, and for how long'. Read-only, additive."""
    import datetime as _dt
    from sqlalchemy import func as F
    from models.database import (Transaction, EvalueBankTxn, EvalueWalletLoad, SBIP02Result)
    today = _dt.date.today()
    order = ["current", "d1", "d3", "d7"]
    labels = {"current": "Current", "d1": "1–2 days", "d3": "3–6 days", "d7": "7+ days"}
    buckets = {k: {"count": 0, "value": 0.0} for k in order}

    def _bucket(d):
        try:
            age = (today - _dt.date.fromisoformat((d or "")[:10])).days
        except Exception:
            return None
        return "current" if age <= 0 else "d1" if age <= 2 else "d3" if age <= 6 else "d7"

    def _add(rows):
        for d, c, amt in rows:
            b = _bucket(d)
            if b:
                buckets[b]["count"] += int(c or 0)
                buckets[b]["value"] = round(buckets[b]["value"] + float(amt or 0), 2)

    def _rng(q, col):
        if date_from:
            q = q.filter(col >= date_from)
        if date_to:
            q = q.filter(col <= date_to)
        return q

    # core ledger — the Open-Items default open set {unmatched, src_assigned}
    _add(_rng(db.query(Transaction.recon_date, F.count(Transaction.id), F.sum(Transaction.amount)).filter(
        Transaction.row_type == "txn", Transaction.recon_date.like("20%"),
        Transaction.recon_status.in_(["unmatched", "src_assigned"])), Transaction.recon_date)
        .group_by(Transaction.recon_date).all())
    # E-Value open bank credits + wallet loads
    _add(_rng(db.query(EvalueBankTxn.txn_date, F.count(EvalueBankTxn.id), F.sum(EvalueBankTxn.amount)).filter(
        EvalueBankTxn.recon_status.in_(["unmatched_bank", "src_assigned", "wrong_amount"])), EvalueBankTxn.txn_date)
        .group_by(EvalueBankTxn.txn_date).all())
    _add(_rng(db.query(EvalueWalletLoad.transaction_date, F.count(EvalueWalletLoad.id), F.sum(EvalueWalletLoad.amount)).filter(
        EvalueWalletLoad.recon_status.in_(["unmatched_load", "src_assigned"])), EvalueWalletLoad.transaction_date)
        .group_by(EvalueWalletLoad.transaction_date).all())
    # SBI Kiosk P02 unmatched bank rows
    _add(_rng(db.query(SBIP02Result.recon_date, F.count(SBIP02Result.id), F.sum(SBIP02Result.bank_amount)).filter(
        SBIP02Result.match_status == "Unmatched"), SBIP02Result.recon_date)
        .group_by(SBIP02Result.recon_date).all())

    total = sum(b["count"] for b in buckets.values())
    total_val = round(sum(b["value"] for b in buckets.values()), 2)
    return {"buckets": [{"bucket": k, "label": labels[k], **buckets[k]} for k in order],
            "total_count": total, "total_value": total_val}


# ── Analytics result cache ────────────────────────────────────────────────────
# build_analytics scans the ledger + SBI/E-Value tables; even with the covering indexes a wide
# range costs a couple of seconds. The result only changes when a recon/upload runs (a few times
# a day), so cache it briefly — single-worker process, so a module dict is safe. The key includes
# the DB bind id so unit tests (each with their own in-memory engine) never share results.
import time as _time
import os as _os
_ANALYTICS_TTL = float(_os.getenv("ANALYTICS_CACHE_TTL", "60"))
_ANALYTICS_CACHE = {}   # (bind_id, date_from, date_to, product, side) -> (expires_at, result)


def _bind_id(db):
    try:
        return id(db.get_bind())
    except Exception:
        return 0


def _cache_get(key):
    hit = _ANALYTICS_CACHE.get(key)
    if hit and hit[0] > _time.time():
        return hit[1]
    return None


def _cache_put(key, val):
    now = _time.time()
    if len(_ANALYTICS_CACHE) > 64:                       # prune expired, then hard-cap
        for k in [k for k, v in _ANALYTICS_CACHE.items() if v[0] <= now]:
            _ANALYTICS_CACHE.pop(k, None)
        if len(_ANALYTICS_CACHE) > 64:
            _ANALYTICS_CACHE.clear()
    _ANALYTICS_CACHE[key] = (now + _ANALYTICS_TTL, val)


def clear_analytics_cache():
    """Invalidate the analytics cache (e.g. after a recon/upload) so the dashboard updates now."""
    _ANALYTICS_CACHE.clear()


def build_analytics(db, date_from=None, date_to=None, product=None, side=None):
    from sqlalchemy import func as F
    from models.database import (Transaction, EvalueBankTxn, EvalueWalletLoad,
                                 SBIP02Result)
    _ck = (_bind_id(db), date_from, date_to, product, side)
    _hit = _cache_get(_ck)
    if _hit is not None:
        return _hit
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
    # E-Value load side is dated by its VALUE date (fallback txn) — the date it
    # reconciles on (behavior-contract item 13); bank side has a single date.
    for model, datecol, sd in ((EvalueBankTxn, EvalueBankTxn.txn_date, "bank"),
                               (EvalueWalletLoad,
                                F.coalesce(F.nullif(EvalueWalletLoad.value_date, ""),
                                           EvalueWalletLoad.transaction_date), "internal")):
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

    # ── 4. BBPS (if populated) — column is transaction_date, not txn_date ──────
    if BbpsBankTxn is not None:
        for model, sd in ((BbpsBankTxn, "bank"), (BbpsInternal, "internal")):
            try:
                dc = model.transaction_date
                bq = db.query(dc, model.recon_status, F.count(model.id), F.sum(model.amount))
                if date_from:
                    bq = bq.filter(dc >= date_from)
                if date_to:
                    bq = bq.filter(dc <= date_to)
                for d, status, cnt, amt in bq.group_by(dc, model.recon_status).all():
                    if not _in_range((d or "")[:10]):
                        continue
                    recs.append(("bbps", sd, d[:10], _statv(status), cnt or 0, float(amt or 0)))
            except Exception:
                logger.warning("analytics: BBPS aggregation skipped", exc_info=True)

    # ── Partner → product-group map (so a DMT bank reads as "DMT · Axis Bank") ──
    # Defined BEFORE filtering because the product filter matches by GROUP.
    from models.database import PartnerConfig
    pcfg = {p.slug: ((p.product or "dmt"), (p.display_name or p.slug))
            for p in db.query(PartnerConfig).all()}

    def _group_of(prod):
        """(group_slug, group_label, bank_label) for a rec key."""
        if prod == "sbi_kiosk":
            return "kiosk", "SBI Kiosk", "SBI Kiosk"
        if prod in ("evalue", "bbps"):
            return prod, _GROUP_LABEL.get(prod, _pretty(prod)), _pretty(prod)
        g, disp = pcfg.get(prod, ("dmt" if prod in ("axis", "fino", "levin", "airtel") else prod, _pretty(prod)))
        return g, _GROUP_LABEL.get(g, _pretty(g)), disp

    # Full product-group universe for the dropdown — captured BEFORE filtering, so
    # picking one product never collapses the list of choices to just that one.
    group_options = {}
    for r in recs:
        g, glabel, _ = _group_of(r[0])
        group_options[g] = glabel

    # ── Optional filters ──────────────────────────────────────────────────────
    # product filter matches the GROUP, so selecting "DMT" aggregates Axis/Fino/
    # Levin/Airtel (what the user thinks of as the product), not a single bank.
    if product:
        recs = [r for r in recs if _group_of(r[0])[0] == product]
    if side in ("bank", "internal"):
        recs = [r for r in recs if r[1] == side]

    # ── Roll up ───────────────────────────────────────────────────────────────
    by_product, by_group, by_side_map, daily_map, products = {}, {}, {"bank": _blank(), "internal": _blank()}, {}, set()
    group_meta = {}      # group_slug → label
    prod_meta = {}       # prod key → (group_slug, group_label, bank_label)
    totals = _blank()

    def _apply(bag, b, cnt, amt):
        bag[b] += cnt
        if b == "matched":
            bag["matched_volume"] = round(bag["matched_volume"] + amt, 2)
        elif b == "unmatched":
            bag["open_volume"] = round(bag["open_volume"] + amt, 2)

    # status-composition of the 'other' bucket, per group / product / overall — so the
    # dashboard can show WHY those rows aren't matched or open (Failed, Duplicate, Fee, …).
    other_g, other_p, other_t = {}, {}, {}
    # Per-side raw transaction counts (BOTH legs, before the pair-dedup below) so the
    # Transactions column can show bank-side AND internal-side totals, not just the
    # deduped pair count. Additive only — the headline `transactions` is unchanged.
    side_tx_t = {"bank": 0, "internal": 0}
    side_tx_p, side_tx_g = {}, {}
    for prod, sd, d, status, cnt, amt in recs:
        products.add(prod)
        if prod not in prod_meta:
            prod_meta[prod] = _group_of(prod)
        g, glabel, _ = prod_meta[prod]
        group_meta[g] = glabel
        b = _bucket(status)
        # by_side always sees BOTH legs — the "bank vs internal" chart is about exactly that.
        _apply(by_side_map.setdefault(sd, _blank()), b, cnt, amt)
        # Count both legs on their true side (pre-dedup) for the per-side breakdown.
        sk = "internal" if sd == "internal" else "bank"
        side_tx_t[sk] += cnt
        side_tx_p.setdefault(prod, {"bank": 0, "internal": 0})[sk] += cnt
        side_tx_g.setdefault(g, {"bank": 0, "internal": 0})[sk] += cnt
        # A matched / amount-mismatch PAIR is ONE transaction represented by two rows —
        # a bank leg and an internal leg. Count it ONCE (the bank leg) in every headline
        # figure so counts and volume aren't doubled. Unmatched legs are distinct orphans
        # (no counterpart), so both sides are kept. EXCEPT internal_matched, which is an
        # internal self-match with NO bank leg — dropping it would zero it out, so keep it.
        if b in ("matched", "mismatch") and sd == "internal" and str(status).lower() != "internal_matched":
            continue
        for bag in (totals, by_product.setdefault(prod, _blank()),
                    by_group.setdefault(g, _blank()), daily_map.setdefault(d, _blank())):
            _apply(bag, b, cnt, amt)
        if b == "other":
            lbl = _status_label(status)
            other_t[lbl] = other_t.get(lbl, 0) + cnt
            other_g.setdefault(g, {})[lbl] = other_g.setdefault(g, {}).get(lbl, 0) + cnt
            other_p.setdefault(prod, {})[lbl] = other_p.setdefault(prod, {}).get(lbl, 0) + cnt

    def _rate(bag):
        m = bag["matched"]; denom = m + bag["unmatched"] + bag["mismatch"]
        return round(m / denom * 100, 1) if denom else 0.0

    def _total(bag):
        return bag["matched"] + bag["unmatched"] + bag["mismatch"] + bag["other"]

    totals["transactions"] = _total(totals)
    totals["bank_transactions"] = side_tx_t["bank"]
    totals["internal_transactions"] = side_tx_t["internal"]
    totals["match_rate"] = _rate(totals)
    totals["other_statuses"] = other_t

    # SBI Kiosk Bank/Int split: its recs are P02-result rows (single 'bank' side), so
    # side_tx has no internal count. Source it from the raw SBI tables — bank statement
    # rows vs BC transaction-report rows — over the same window. Additive only; NOT fed
    # into totals/by_side/pair-dedup (headline numbers stay untouched).
    _kiosk_bank = _kiosk_int = 0
    if product in (None, "", "kiosk"):          # only compute when kiosk is in scope
        def _sbi_count(model):
            from sqlalchemy import func as _F
            qq = db.query(_F.count(model.id)).filter(
                model.txn_date.isnot(None), model.txn_date.like("20%"))
            if date_from: qq = qq.filter(model.txn_date >= date_from)
            if date_to:   qq = qq.filter(model.txn_date <= date_to)
            return qq.scalar() or 0
        try:
            from models.database import SBIBankTransaction, SBITxnReport
            if side != "internal":              # honour the bank/internal side filter
                _kiosk_bank = _sbi_count(SBIBankTransaction)
            if side != "bank":
                _kiosk_int = _sbi_count(SBITxnReport)
        except Exception:
            pass

    def _side_tx(container, key, is_kiosk):
        # kiosk sides come from the SBI source tables (above); everyone else from side_tx.
        if is_kiosk:
            return _kiosk_bank, _kiosk_int
        d = container.get(key, {})
        return d.get("bank", 0), d.get("internal", 0)

    by_product_list = sorted(
        ({"product": p, "label": prod_meta[p][2],
          "group": prod_meta[p][0], "group_label": prod_meta[p][1],
          **v, "transactions": _total(v), "match_rate": _rate(v),
          "bank_transactions": _side_tx(side_tx_p, p, p == "sbi_kiosk")[0],
          "internal_transactions": _side_tx(side_tx_p, p, p == "sbi_kiosk")[1],
          "other_statuses": other_p.get(p, {})}
         for p, v in by_product.items()),
        key=lambda x: (x["group_label"], -x["transactions"]))

    by_group_list = sorted(
        ({"group": g, "label": group_meta[g], **v,
          "transactions": _total(v), "match_rate": _rate(v),
          "bank_transactions": _side_tx(side_tx_g, g, g == "kiosk")[0],
          "internal_transactions": _side_tx(side_tx_g, g, g == "kiosk")[1],
          "other_statuses": other_g.get(g, {})}
         for g, v in by_group.items()),
        key=lambda x: -x["transactions"])

    by_side_list = [{"side": s, **by_side_map[s], "transactions": _total(by_side_map[s]),
                     "match_rate": _rate(by_side_map[s])}
                    for s in ("bank", "internal") if _total(by_side_map[s]) > 0]

    by_status_list = [{"status": k, "label": k.title(), "count": totals[k]}
                      for k in ("matched", "unmatched", "mismatch", "other") if totals[k] > 0]

    daily_list = [{"date": d, **daily_map[d], "transactions": _total(daily_map[d]),
                   "match_rate": _rate(daily_map[d])}
                  for d in sorted(daily_map)]

    # products dropdown: one option per product GROUP (DMT, QR, SBI Kiosk, …) — the
    # value is the group slug the filter matches, so selecting "DMT" shows all its
    # banks. Built from the pre-filter universe so the choices never collapse.
    prod_options = [{"product": g, "label": glabel}
                    for g, glabel in sorted(group_options.items(), key=lambda x: x[1])]

    result = {
        "date_from": date_from, "date_to": date_to,
        "product": product, "side": side,
        "totals": totals,
        "by_product": by_product_list,
        "by_group": by_group_list,
        "by_status": by_status_list,
        "by_side": by_side_list,
        "daily": daily_list,
        "products": prod_options,
        # Additive: the 4 SBI Kiosk process recons (P01–P04) for the expandable breakdown.
        # NOT part of totals/by_group, so headline numbers are unchanged.
        "kiosk_processes": _kiosk_processes(db, date_from, date_to) if product in (None, "", "kiosk") else [],
        # Additive: ageing of open (unmatched) items — count + ₹ per age bucket.
        "open_ageing": _open_ageing(db, date_from, date_to),
    }
    _cache_put(_ck, result)
    return result


def _statv(status) -> str:
    """Enum-or-str → plain string (MySQL returns str, SQLite may return the enum)."""
    return str(getattr(status, "value", status) or "")
