"""
routes/report_library.py — the product-agnostic Report Library.

ONE registry + TWO endpoints give every product a standard pack of reports:

  GET /api/reports/library          → products + their available reports
  GET /api/reports/library/run      → one styled Excel workbook

- CORE-LEDGER products (dmt, aeps, pg, qr, digikhata, indonepal — and ANY future
  product that appears in PartnerConfig.product) get the generic Transaction-based
  pack AUTOMATICALLY: zero code per new product.
- MODULE products plug in adapters: kiosk delegates to the SBI report library
  (routes/sbi_kiosk.py); E-Value and BBPS get thin packs over their own tables.

Everything is READ-ONLY. Counting is deliberately per-recon_status (explicit
columns, no invented buckets) so the numbers always mirror Open Items; the only
"matched" roll-ups reuse the same status sets the Reports page already uses.
The core ledger's `recon_date` can hold the sentinel 'auto' / 'auto (multi-date)'
— date-scoped core reports guard with LIKE '20%' so the sentinel neither wins a
max() nor leaks into lexicographic ranges.
"""
import io
import datetime
from typing import Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from models.database import (
    get_db, Transaction, ReconStatus, PartnerConfig,
    EvalueBankTxn, EvalueWalletLoad, BbpsBankTxn, BbpsInternal,
    AepsSettlement, AepsAnomaly, QRSettlement, QRChargeback,
)
from core.auth import get_current_user
from routes.sbi_kiosk import _sheet  # shared styled-sheet writer

router = APIRouter(prefix="/api/reports/library", tags=["report-library"])

_sv = lambda v: getattr(v, "value", v)  # Enum-or-str → str (driver-dependent)


# ── Registry ───────────────────────────────────────────────────────────────────

CORE_REPORTS = [
    {"id": "daily_pack",    "label": "Daily Recon Pack",       "scope": "date",
     "desc": "One workbook for the day: overview per partner, matched rows, open items both sides, exceptions, SRC log."},
    {"id": "exceptions",    "label": "Exceptions / Work List", "scope": "range",
     "desc": "Only what needs action: open bank, open internal, amount mismatches, duplicates."},
    {"id": "mis_summary",   "label": "MIS Summary (trend)",    "scope": "range",
     "desc": "Per date × partner: counts by status, match rate and open amounts — management trend view."},
    {"id": "partner_split", "label": "Partner / Bank Split",   "scope": "range",
     "desc": "Totals per partner over the period — volumes, statuses, amounts."},
    {"id": "ageing",        "label": "Ageing of Open Items",   "scope": "all",
     "desc": "Open items bucketed by how long they have been open (0-1 / 2-3 / 4-7 / 8-15 / 15+ days)."},
    {"id": "reversals",     "label": "Reversals Report",       "scope": "range",
     "desc": "All reversal-matched pairs (original + reversal legs, linked by match ID)."},
    {"id": "src_log",       "label": "SRC Disposition Log",    "scope": "range",
     "desc": "Every SRC-tagged row: code, note, and the row it disposes."},
    {"id": "manual_log",    "label": "Manual Match Log",       "scope": "range",
     "desc": "Every manually matched pair (incl. amount-mismatch flags), with match IDs and override notes."},
]

MODULE_REPORTS = [   # E-Value + BBPS share this thin standard pack
    {"id": "daily_pack",  "label": "Recon Pack",             "scope": "range",
     "desc": "Overview by account/provider + full rows both sides + exceptions + SRC log."},
    {"id": "exceptions",  "label": "Exceptions / Work List", "scope": "range",
     "desc": "Unmatched rows on both sides — only what needs action."},
    {"id": "mis_summary", "label": "MIS Summary (trend)",    "scope": "range",
     "desc": "Per date: counts by status and match rate."},
    {"id": "src_log",     "label": "SRC Disposition Log",    "scope": "range",
     "desc": "Every SRC-tagged row: code, note, who."},
]

_CORE_LABELS = {
    "dmt": "DMT (All Banks)", "aeps": "AePS Cashout", "pg": "Accept Payment (PG)",
    "qr": "QR Collection", "digikhata": "Digikhata (PPI)", "indonepal": "Indo-Nepal",
}

# ── Per-product PROCESS reports ────────────────────────────────────────────────
# Products are NOT uniform: beyond the shared ledger pack, each product's own
# processes get their own report structures here. A future product with bespoke
# processes just adds an entry + builder — the UI picks it up automatically.
PRODUCT_EXTRA_REPORTS = {
    "aeps": [
        {"id": "settlement", "label": "T+ Settlement Register", "scope": "range",
         "desc": "AePS settlement calculations: settlement vs transaction amount, anomalies, hold/recovery, TWAFA, CD."},
        {"id": "anomalies", "label": "Anomaly Register", "scope": "range",
         "desc": "Three-way anomaly rows (RRN, amount, original vs 3-way status, active flag)."},
    ],
    "qr": [
        {"id": "settlement", "label": "Settlement Batches", "scope": "range",
         "desc": "QR settlement batches: gross, fees & taxes, early-settlement charges, net payout, payout status."},
        {"id": "chargebacks", "label": "Chargebacks", "scope": "range",
         "desc": "QR chargeback/adjustment cases with TAT dates and status."},
    ],
    "pg": [
        {"id": "settlement", "label": "PG Settlement (gross/fees/net)", "scope": "range",
         "desc": "Per-date PayU settlement: transactions, gross, gateway fees, net settlement."},
    ],
    "evalue": [
        {"id": "interbank", "label": "Interbank Transfers", "scope": "range",
         "desc": "Interbank-matched legs across the 8 accounts, linked by match ID."},
    ],
    "bbps": [
        {"id": "refunds", "label": "Refund Lifecycle", "scope": "range",
         "desc": "Refunded rows incl. refunded-but-success exceptions, both sides."},
    ],
}
_MODULE_PRODUCTS = {"kiosk", "evalue", "bbps"}   # own tables, never Transaction

# Statuses the Reports page already treats as "matched" for rate purposes.
_CORE_MATCHED = ("matched", "manual_matched")
_CORE_OPENISH = ("unmatched", "src_assigned", "amount_mismatch")

_EV_MATCHED = ("matched_online", "matched_cash", "matched_manual", "interbank_matched")
# BBPS reconciled = matched OR failed_refunded (bbps_engine.RECON_OK). 'reconciled' is a
# summary-only aggregate key, never a per-row status, so it matched zero rows before.
_BBPS_MATCHED = ("matched", "failed_refunded")


def _core_partner_slugs(db, product: str):
    """Partner slugs for a core product — PartnerConfig.product first (future-proof),
    falling back to the Reports page's static expansion (e.g. dmt → its banks)."""
    slugs = [p.slug for p in db.query(PartnerConfig).filter(PartnerConfig.product == product).all()]
    if not slugs:
        from routes.reports import _partner_slugs
        slugs = _partner_slugs(product) or []
    return slugs


def _products(db):
    """Every product that can serve reports — core (dynamic) + the three modules."""
    core = {p.product or "dmt" for p in db.query(PartnerConfig).all()} - _MODULE_PRODUCTS
    core |= set(_CORE_LABELS)          # defaults even before partners are seeded
    out = []
    for prod in sorted(core):
        out.append({"id": prod, "kind": "core",
                    "label": _CORE_LABELS.get(prod, prod.replace("_", " ").title()),
                    "reports": CORE_REPORTS + PRODUCT_EXTRA_REPORTS.get(prod, [])})
    out.append({"id": "evalue", "kind": "module", "label": "E-Value",
                "reports": MODULE_REPORTS + PRODUCT_EXTRA_REPORTS.get("evalue", [])})
    out.append({"id": "bbps",   "kind": "module", "label": "BBPS",
                "reports": MODULE_REPORTS + PRODUCT_EXTRA_REPORTS.get("bbps", [])})
    from routes.sbi_kiosk import _SBI_REPORTS
    out.append({"id": "kiosk",  "kind": "module", "label": "SBI Kiosk", "reports": _SBI_REPORTS})
    return out


@router.get("")
def list_library(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    return {"products": _products(db)}


# ── Core-ledger generic builders ───────────────────────────────────────────────

_TXN_COLS = ["side", "partner", "recon_date", "transaction_date", "eko_tid",
             "tracking_number", "utr_number", "amount", "dr_cr", "status", "row_type",
             "recon_status", "match_id", "src_code", "src_note", "csp_code", "bank_description"]


def _txn_dicts(rows):
    out = []
    for t in rows:
        d = {c: getattr(t, c) for c in _TXN_COLS}
        d["recon_status"] = _sv(d["recon_status"])
        out.append(d)
    return out


def _core_q(db, slugs, recon_date=None, date_from=None, date_to=None):
    q = db.query(Transaction).filter(Transaction.partner.in_(slugs))
    if recon_date:
        q = q.filter(Transaction.recon_date == recon_date)
    elif date_from or date_to:
        q = q.filter(Transaction.recon_date.like("20%"))   # exclude 'auto' sentinel
        if date_from: q = q.filter(Transaction.recon_date >= date_from)
        if date_to:   q = q.filter(Transaction.recon_date <= date_to)
    return q


def _core_overview(rows, keys=("partner",)):
    """Explicit per-status pivot per key — mirrors Open Items, no invented buckets."""
    agg = {}
    statuses = set()
    for t in rows:
        k = tuple((t.get(c) if isinstance(t, dict) else getattr(t, c)) or "" for c in keys)
        g = t if isinstance(t, dict) else None
        side = (g["side"] if g else t.side) or ""
        rs = _sv(g["recon_status"] if g else t.recon_status) or "(none)"
        amt = (g["amount"] if g else t.amount) or 0
        statuses.add(rs)
        a = agg.setdefault(k, {"internal_total": 0, "bank_total": 0, "amt_matched": 0.0,
                               "amt_open_bank": 0.0, "amt_open_internal": 0.0, "by": {}})
        a["internal_total" if side == "internal" else "bank_total"] += 1
        a["by"][rs] = a["by"].get(rs, 0) + 1
        if rs in _CORE_MATCHED and side == "internal":
            a["amt_matched"] = round(a["amt_matched"] + amt, 2)
        if rs in _CORE_OPENISH:
            f = "amt_open_bank" if side == "bank" else "amt_open_internal"
            a[f] = round(a[f] + amt, 2)
    status_cols = sorted(statuses)
    out = []
    for k in sorted(agg):
        a = agg[k]
        matched = sum(a["by"].get(s, 0) for s in _CORE_MATCHED)
        row = {c: v for c, v in zip(keys, k)}
        row.update({"internal": a["internal_total"], "bank": a["bank_total"]})
        row.update({s: a["by"].get(s, 0) for s in status_cols})
        row["match_rate_pct"] = round(matched / a["internal_total"] * 100, 1) if a["internal_total"] else None
        row.update({"amt_matched": a["amt_matched"], "amt_open_bank": a["amt_open_bank"],
                    "amt_open_internal": a["amt_open_internal"]})
        out.append(row)
    cols = list(keys) + ["internal", "bank"] + status_cols + \
           ["match_rate_pct", "amt_matched", "amt_open_bank", "amt_open_internal"]
    return out, cols


def _build_core(db, product, t, rd, df_, dt_, w):
    slugs = _core_partner_slugs(db, product)
    if not slugs:
        raise HTTPException(status_code=404, detail=f"No partners configured for product '{product}'")

    if t == "daily_pack":
        rows = _core_q(db, slugs, rd).all()
        dicts = _txn_dicts(rows)
        ov, ovcols = _core_overview(dicts)
        _sheet(w, "Overview", ov, ovcols)
        _sheet(w, "Matched", [d for d in dicts if d["recon_status"] in _CORE_MATCHED], _TXN_COLS)
        _sheet(w, "Open Bank", [d for d in dicts if d["side"] == "bank"
                                and d["recon_status"] in ("unmatched", "src_assigned")], _TXN_COLS)
        _sheet(w, "Open Internal", [d for d in dicts if d["side"] == "internal"
                                    and d["recon_status"] in ("unmatched", "src_assigned")], _TXN_COLS)
        _sheet(w, "Exceptions", [d for d in dicts if d["recon_status"]
                                 in ("amount_mismatch", "duplicate")], _TXN_COLS)
        _sheet(w, "SRC Log", [d for d in dicts if d["src_code"]], _TXN_COLS)

    elif t == "exceptions":
        dicts = _txn_dicts(_core_q(db, slugs, rd, df_, dt_).filter(
            Transaction.recon_status.in_([ReconStatus.unmatched, ReconStatus.src_assigned,
                                          ReconStatus.amount_mismatch, ReconStatus.duplicate])).all())
        _sheet(w, "Open Bank", [d for d in dicts if d["side"] == "bank"
                                and d["recon_status"] in ("unmatched", "src_assigned")], _TXN_COLS)
        _sheet(w, "Open Internal", [d for d in dicts if d["side"] == "internal"
                                    and d["recon_status"] in ("unmatched", "src_assigned")], _TXN_COLS)
        _sheet(w, "Amount Mismatch", [d for d in dicts if d["recon_status"] == "amount_mismatch"], _TXN_COLS)
        _sheet(w, "Duplicates", [d for d in dicts if d["recon_status"] == "duplicate"], _TXN_COLS)

    elif t == "mis_summary":
        dicts = _txn_dicts(_core_q(db, slugs, rd, df_, dt_).all())
        grid, gcols = _core_overview(dicts, keys=("recon_date", "partner"))
        _sheet(w, "By Date x Partner", grid, gcols)
        tot, tcols = _core_overview(dicts, keys=("partner",))
        _sheet(w, "Totals by Partner", tot, tcols)

    elif t == "partner_split":
        dicts = _txn_dicts(_core_q(db, slugs, rd, df_, dt_).all())
        tot, tcols = _core_overview(dicts, keys=("partner",))
        _sheet(w, "Partner Split", tot, tcols)

    elif t == "ageing":
        # Mirrors /reports/ageing: OPEN = unmatched only, age from recon_date.
        rows = db.query(Transaction).filter(
            Transaction.partner.in_(slugs),
            Transaction.recon_status == ReconStatus.unmatched,
            Transaction.recon_date.like("20%")).all()
        today = datetime.date.today()
        buckets = [("0-1 days", 0, 1), ("2-3 days", 2, 3), ("4-7 days", 4, 7),
                   ("8-15 days", 8, 15), ("15+ days", 16, 10 ** 6)]
        det = []
        for t_ in rows:
            try:
                age = (today - datetime.date.fromisoformat(t_.recon_date[:10])).days
            except Exception:
                continue
            b = next((n for n, lo, hi in buckets if lo <= age <= hi), "15+ days")
            d = {c: getattr(t_, c) for c in _TXN_COLS}
            d["recon_status"] = _sv(d["recon_status"]); d["age_days"] = age; d["bucket"] = b
            det.append(d)
        summ = {}
        for d in det:
            s = summ.setdefault((d["bucket"], d["partner"]), {"bucket": d["bucket"], "partner": d["partner"],
                                                              "items": 0, "amount": 0.0})
            s["items"] += 1; s["amount"] = round(s["amount"] + (d["amount"] or 0), 2)
        order = {n: i for i, (n, _, _) in enumerate(buckets)}
        _sheet(w, "Ageing Summary", sorted(summ.values(), key=lambda s: (order.get(s["bucket"], 9), s["partner"])),
               ["bucket", "partner", "items", "amount"])
        det.sort(key=lambda d: -d["age_days"])
        _sheet(w, "Open Items by Age", det, ["bucket", "age_days"] + _TXN_COLS)

    elif t == "reversals":
        dicts = _txn_dicts(_core_q(db, slugs, rd, df_, dt_).filter(
            Transaction.recon_status == ReconStatus.reversal_matched).all())
        dicts.sort(key=lambda d: (d["match_id"] or "", d["side"]))
        _sheet(w, "Reversal Pairs", dicts, _TXN_COLS)

    elif t == "src_log":
        dicts = _txn_dicts(_core_q(db, slugs, rd, df_, dt_).filter(
            Transaction.src_code.isnot(None), Transaction.src_code != "").all())
        _sheet(w, "SRC Log", dicts, _TXN_COLS)

    elif t == "manual_log":
        dicts = []
        for t_ in _core_q(db, slugs, rd, df_, dt_).filter(Transaction.recon_status.in_(
                [ReconStatus.manual_matched, ReconStatus.amount_mismatch])).all():
            d = {c: getattr(t_, c) for c in _TXN_COLS}
            d["recon_status"] = _sv(d["recon_status"])
            d["override_note"], d["override_by"] = t_.override_note, t_.override_by
            dicts.append(d)
        dicts.sort(key=lambda d: (d["match_id"] or "", d["side"]))
        _sheet(w, "Manual Matches", dicts, _TXN_COLS + ["override_note", "override_by"])


# ── E-Value / BBPS adapters (same standard pack over their own tables) ─────────

_EV_BANK_COLS = ["reco_acc_no", "bank_name", "txn_date", "description", "dr_cr", "amount",
                 "utr", "ref_no", "recon_status", "match_id", "match_note", "src_code", "src_note"]
_EV_LOAD_COLS = ["reco_acc_no", "transaction_date", "eko_trxn_id", "csp_code", "merchant_name",
                 "amount", "mode", "utr_number", "status", "recon_status", "match_id",
                 "match_note", "src_code", "src_note"]
_BB_BANK_COLS = ["provider", "transaction_date", "client_ref", "operator_ref", "order_id",
                 "amount", "status", "service_name", "recon_status", "match_id", "src_code", "src_note"]
_BB_INT_COLS = ["provider", "transaction_date", "eko_trxn_id", "csp_code", "amount", "status",
                "tracking_number", "is_refunded", "recon_status", "match_id", "src_code", "src_note"]


def _mod_rows(db, model, date_col, cols, df_, dt_):
    q = db.query(model)
    if df_: q = q.filter(getattr(model, date_col) >= df_)
    if dt_: q = q.filter(getattr(model, date_col) <= dt_)
    out = []
    for r in q.all():
        d = {c: getattr(r, c) for c in cols}
        d["recon_status"] = _sv(d.get("recon_status")) or "(not run)"
        out.append(d)
    return out


def _mod_overview(rows, key, date_col, matched_set, by_date=False):
    agg, statuses = {}, set()
    for d in rows:
        k = ((d.get(date_col) or "")[:10], d.get(key) or "") if by_date else (d.get(key) or "",)
        rs = d["recon_status"]
        statuses.add(rs)
        a = agg.setdefault(k, {"total": 0, "by": {}, "amount": 0.0})
        a["total"] += 1; a["by"][rs] = a["by"].get(rs, 0) + 1
        a["amount"] = round(a["amount"] + (d.get("amount") or 0), 2)
    scols = sorted(statuses)
    keys = (["date", key] if by_date else [key])
    out = []
    for k in sorted(agg):
        a = agg[k]
        matched = sum(a["by"].get(s, 0) for s in matched_set)
        row = dict(zip(keys, k))
        row.update({"rows": a["total"], "amount": a["amount"]})
        row.update({s: a["by"].get(s, 0) for s in scols})
        row["match_rate_pct"] = round(matched / a["total"] * 100, 1) if a["total"] else None
        out.append(row)
    return out, keys + ["rows", "amount"] + scols + ["match_rate_pct"]


def _build_module(db, product, t, df_, dt_, w):
    if product == "evalue":
        bank = _mod_rows(db, EvalueBankTxn, "txn_date", _EV_BANK_COLS + ["recon_status"], df_, dt_)
        data = _mod_rows(db, EvalueWalletLoad, "transaction_date", _EV_LOAD_COLS + ["recon_status"], df_, dt_)
        key_b, key_d, matched = "reco_acc_no", "reco_acc_no", _EV_MATCHED
        bcols, dcols, bname, dname = _EV_BANK_COLS, _EV_LOAD_COLS, "Bank Credits", "Wallet Loads"
    else:
        bank = _mod_rows(db, BbpsBankTxn, "transaction_date", _BB_BANK_COLS + ["recon_status"], df_, dt_)
        data = _mod_rows(db, BbpsInternal, "transaction_date", _BB_INT_COLS + ["recon_status"], df_, dt_)
        key_b, key_d, matched = "provider", "provider", _BBPS_MATCHED
        bcols, dcols, bname, dname = _BB_BANK_COLS, _BB_INT_COLS, "Operator Rows", "Internal Rows"

    if t == "daily_pack":
        ovb, ovbc = _mod_overview(bank, key_b, "txn_date", matched)
        ovd, ovdc = _mod_overview(data, key_d, "transaction_date", matched)
        _sheet(w, f"Overview {bname}"[:31], ovb, ovbc)
        _sheet(w, f"Overview {dname}"[:31], ovd, ovdc)
        _sheet(w, bname, bank, bcols)
        _sheet(w, dname, data, dcols)
        _sheet(w, "Exceptions", [d for d in bank + data if d["recon_status"] not in matched
                                 and d["recon_status"] not in ("src_assigned",)], sorted(set(bcols) | set(dcols)))
        _sheet(w, "SRC Log", [d for d in bank + data if d.get("src_code")], sorted(set(bcols) | set(dcols)))
    elif t == "exceptions":
        _sheet(w, f"Open {bname}"[:31], [d for d in bank if d["recon_status"] not in matched
                                         and d["recon_status"] != "src_assigned"], bcols)
        _sheet(w, f"Open {dname}"[:31], [d for d in data if d["recon_status"] not in matched
                                         and d["recon_status"] != "src_assigned"], dcols)
    elif t == "mis_summary":
        gb, gbc = _mod_overview(bank, key_b, "txn_date" if product == "evalue" else "transaction_date",
                                matched, by_date=True)
        gd, gdc = _mod_overview(data, key_d, "transaction_date", matched, by_date=True)
        _sheet(w, f"{bname} by Date"[:31], gb, gbc)
        _sheet(w, f"{dname} by Date"[:31], gd, gdc)
    elif t == "src_log":
        _sheet(w, "SRC Log", [d for d in bank + data if d.get("src_code")],
               sorted(set(bcols) | set(dcols)))
    else:
        raise HTTPException(status_code=400, detail=f"Unknown report '{t}' for {product}")


# ── Per-product PROCESS report builders ────────────────────────────────────────

def _dump(db, model, cols, date_col, df_, dt_, order_col=None):
    q = db.query(model)
    if df_: q = q.filter(getattr(model, date_col) >= df_)
    if dt_: q = q.filter(getattr(model, date_col) <= dt_)
    if order_col is not None:
        q = q.order_by(order_col)
    return [{c: getattr(r, c) for c in cols} for r in q.all()]


def _build_extra(db, prod, t, df_, dt_, w):
    if prod == "aeps" and t == "settlement":
        cols = ["upload_date", "created_date", "settle_timestamp", "settlement_amount",
                "transaction_amount", "anomaly_amount", "hold_recovery", "twafa_per_txn",
                "twafa_count", "twafa_total", "cd_amount", "reference_number", "cms_number",
                "status", "status_message", "service_type"]
        _sheet(w, "T+ Settlements", _dump(db, AepsSettlement, cols, "upload_date", df_, dt_,
                                          AepsSettlement.upload_date), cols)
    elif prod == "aeps" and t == "anomalies":
        cols = ["upload_date", "rrn", "amount", "original_status", "threeway_status",
                "txn_timestamp", "created_timestamp", "service_type", "active_flag"]
        _sheet(w, "Anomalies", _dump(db, AepsAnomaly, cols, "upload_date", df_, dt_,
                                     AepsAnomaly.upload_date), cols)
    elif prod == "qr" and t == "settlement":
        cols = ["payment_date", "settlement_id", "gross_amount", "fees", "fees_tax",
                "early_settlement_fees", "early_settlement_tax", "net_settlement", "mode",
                "payment_status", "payout_status", "payout_remarks", "bank_ref_number", "rrn"]
        rows = _dump(db, QRSettlement, cols, "payment_date", df_, dt_, QRSettlement.payment_date)
        _sheet(w, "Settlement Batches", rows, cols)
        daily = {}
        for r in rows:
            d = daily.setdefault((r["payment_date"] or "")[:10],
                                 {"date": (r["payment_date"] or "")[:10], "batches": 0,
                                  "gross": 0.0, "fees_all": 0.0, "net": 0.0})
            d["batches"] += 1
            d["gross"] = round(d["gross"] + (r["gross_amount"] or 0), 2)
            d["fees_all"] = round(d["fees_all"] + sum((r[f] or 0) for f in
                                  ("fees", "fees_tax", "early_settlement_fees", "early_settlement_tax")), 2)
            d["net"] = round(d["net"] + (r["net_settlement"] or 0), 2)
        _sheet(w, "Daily Totals", [daily[k] for k in sorted(daily)],
               ["date", "batches", "gross", "fees_all", "net"])
    elif prod == "qr" and t == "chargebacks":
        cols = ["case_date", "adj_type", "txn_date", "upi_txn_id", "rrn", "adj_amount",
                "tat_date", "status", "notes", "created_by"]
        _sheet(w, "Chargebacks", _dump(db, QRChargeback, cols, "case_date", df_, dt_,
                                       QRChargeback.case_date), cols)
    elif prod == "pg" and t == "settlement":
        # Same computation as /recon/pg-settlement-summary: PayU bank side per date.
        q = db.query(
            Transaction.recon_date,
            func.count(Transaction.id).label("txns"),
            func.sum(Transaction.amount).label("gross"),
            func.sum(Transaction.net_amount).label("net"),
        ).filter(Transaction.partner == "pg", Transaction.side == "bank")
        q = q.filter(Transaction.recon_date.like("20%"))
        if df_: q = q.filter(Transaction.recon_date >= df_)
        if dt_: q = q.filter(Transaction.recon_date <= dt_)
        rows = []
        for d, n, gross, net in q.group_by(Transaction.recon_date).order_by(Transaction.recon_date).all():
            gross, net = round(gross or 0, 2), round(net or 0, 2)
            rows.append({"date": d, "transactions": n, "gross_amount": gross,
                         "gateway_fees": round(gross - net, 2), "net_settlement": net})
        _sheet(w, "PG Settlement", rows,
               ["date", "transactions", "gross_amount", "gateway_fees", "net_settlement"])
    elif prod == "evalue" and t == "interbank":
        rows = []
        for m, cols, side in ((EvalueBankTxn, _EV_BANK_COLS, "bank"),
                              (EvalueWalletLoad, _EV_LOAD_COLS, "load")):
            for r in db.query(m).filter(m.recon_status == "interbank_matched").all():
                d = {c: getattr(r, c) for c in cols}
                d["side"] = side
                rows.append(d)
        rows.sort(key=lambda d: (d.get("match_id") or "", d["side"]))
        _sheet(w, "Interbank Legs", rows, ["side"] + sorted(set(_EV_BANK_COLS) | set(_EV_LOAD_COLS)))
    elif prod == "bbps" and t == "refunds":
        ints = _mod_rows(db, BbpsInternal, "transaction_date", _BB_INT_COLS, df_, dt_)
        banks = _mod_rows(db, BbpsBankTxn, "transaction_date", _BB_BANK_COLS, df_, dt_)
        _sheet(w, "Internal Refunds",
               [d for d in ints if d.get("is_refunded") or "refund" in (d["recon_status"] or "")],
               _BB_INT_COLS)
        _sheet(w, "Bank Refund Status",
               [d for d in banks if "refund" in (d["recon_status"] or "")], _BB_BANK_COLS)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown report '{t}' for {prod}")


# ── Run endpoint ───────────────────────────────────────────────────────────────

@router.get("/run")
def run_library_report(
    product: str = Query(...),
    type: str = Query(...),
    recon_date: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    prod, t = (product or "").lower(), (type or "").lower()

    if prod == "kiosk":   # delegate to the SBI report library (single source of truth)
        from routes.sbi_kiosk import sbi_report
        return sbi_report(type=t, recon_date=recon_date, date_from=date_from,
                          date_to=date_to, db=db, current_user=current_user)

    extras = {r["id"] for r in PRODUCT_EXTRA_REPORTS.get(prod, [])}
    valid = {r["id"] for r in (MODULE_REPORTS if prod in ("evalue", "bbps") else CORE_REPORTS)} | extras
    if t not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown report '{t}' for {prod}")

    used_date = recon_date
    if prod not in ("evalue", "bbps") and t not in extras:
        slugs = _core_partner_slugs(db, prod)
        scope = next(r["scope"] for r in CORE_REPORTS if r["id"] == t)
        if scope == "date":
            if used_date and not _core_q(db, slugs, used_date).first():
                used_date = None
            if not used_date and slugs:
                used_date = db.query(func.max(Transaction.recon_date)).filter(
                    Transaction.partner.in_(slugs), Transaction.recon_date.like("20%")).scalar()

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as w:
        if t in extras:
            _build_extra(db, prod, t, date_from, date_to, w)
        elif prod in ("evalue", "bbps"):
            _build_module(db, prod, t, date_from, date_to, w)
        else:
            _build_core(db, prod, t, used_date, date_from, date_to, w)
    output.seek(0)

    tag = used_date or (f"{date_from or 'start'}_to_{date_to or 'end'}"
                        if (date_from or date_to) else "all")
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{prod}_{t}_{tag}.xlsx"',
            "X-Recon-Date": str(tag),
            "Access-Control-Expose-Headers": "X-Recon-Date",
        },
    )
