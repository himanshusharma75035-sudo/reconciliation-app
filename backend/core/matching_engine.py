"""
Reconciliation Matching Engine
Supports rule-based matching for Airtel and Fino partners.
Rules are applied in priority order; first match wins.
"""
import json
import uuid
import threading
import functools
import pandas as pd
from typing import List, Dict, Tuple, Optional
from sqlalchemy.orm import Session
from models.database import Transaction, ReconRun, MatchRule, ReconStatus, generate_id
import datetime

# Serialises reconciliation runs within this process so two simultaneous runs
# can never read the same match-ID sequence start (race condition fix).
_RECON_LOCK = threading.RLock()


def _serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _RECON_LOCK:
            return fn(*args, **kwargs)
    return wrapper


# ── Bank-specific prefix map ──────────────────────────────────────────────────
# Each bank gets its own series prefix so IDs are clearly identifiable.
# Add new banks here as they are onboarded.
PARTNER_PREFIX = {
    "fino":       "FNO",
    "airtel":     "AIR",
    "aeps":       "APS",
    "pg":         "PGW",
    "axis":       "AXS",
    "levin":      "LVN",
    "digikhata":  "DGK",
    "indonepal":  "INP",
    "qr":         "QRC",
    "manual":     "MAN",   # manual matches with no clear partner
}


def _get_prefix(partner: str, db=None) -> str:
    """
    Return the match prefix for a partner.
    Checks hardcoded PARTNER_PREFIX first, then PartnerConfig table in DB.
    This allows new partners added via admin panel to get their own prefix.
    """
    if partner.lower() in PARTNER_PREFIX:
        return PARTNER_PREFIX[partner.lower()]
    if db is not None:
        try:
            from models.database import PartnerConfig
            p = db.query(PartnerConfig).filter(PartnerConfig.slug == partner.lower()).first()
            if p and p.match_prefix:
                return p.match_prefix
        except Exception:
            pass
    return partner[:3].upper()


def _make_match_id(partner: str, recon_date: str, seq: int) -> str:
    """
    Build a sequential, human-readable match ID.
    Format : {PREFIX}-{YYYYMMDD}-{NNNN}
    Examples:
      FNO-20260415-0001  (Fino,   15 Apr 2026, 1st match)
      AIR-20260415-0043  (Airtel, 15 Apr 2026, 43rd match)
    Prefix is bank-specific so series never overlap across banks.
    """
    prefix = _get_prefix(partner)
    date_compact = recon_date.replace("-", "")[:8] if recon_date else "00000000"
    return f"{prefix}-{date_compact}-{seq:04d}"


def _next_seq(partner: str, recon_date: str, db: Session) -> int:
    """
    Return the next available sequence number for this partner + date series.
    Uses MAX(existing seq) + 1 (not COUNT) so unmatching/deleting a pair can
    never cause a later run to reissue an existing match ID.
    """
    prefix = _get_prefix(partner, db)
    date_compact = recon_date.replace("-", "")[:8] if recon_date else "00000000"
    series_prefix = f"{prefix}-{date_compact}-"
    rows = db.query(Transaction.match_id).filter(
        Transaction.match_id.like(f"{series_prefix}%"),
        Transaction.match_id.isnot(None)
    ).distinct().all()
    max_seq, plen = 0, len(series_prefix)
    for (mid,) in rows:
        try:
            n = int(str(mid)[plen:plen + 4])
            if n > max_seq:
                max_seq = n
        except (ValueError, TypeError):
            continue
    return max_seq + 1


# ─── Default Rules ────────────────────────────────────────────────────────────
DEFAULT_RULES = {
    # Airtel bank statement fields:
    #   eko_tid          = Partner_txn_id  (matches dump's eko_trxn_id — 1:1 perfect)
    #   tracking_number  = RRN             (matches dump's TrackingNumber — 1:1 perfect)
    #   utr_number       = APB_TXN_id      (bank's own ref; used for reversal matching only)
    # Amount: Original Amount (NOT Transaction Amount which includes Charges+GST)
    "airtel": [
        {"name": "TID + Tracking", "fields": ["eko_tid", "tracking_number"], "priority": 1},
        {"name": "TID Only",       "fields": ["eko_tid"],                    "priority": 2},
        {"name": "Tracking Only",  "fields": ["tracking_number"],            "priority": 3},
    ],
    "fino": [
        {"name": "TID + Tracking + UTR", "fields": ["eko_tid", "tracking_number", "utr_number"], "priority": 1},
        {"name": "TID + Tracking",       "fields": ["eko_tid", "tracking_number"],               "priority": 2},
        {"name": "TID + UTR",            "fields": ["eko_tid", "utr_number"],                    "priority": 3},
        {"name": "TID Only",             "fields": ["eko_tid"],                                  "priority": 4},
        # Fallback: Fino IMPS bank descriptions only yield tracking/UTR (no TID in single-segment IMPS)
        {"name": "Tracking Only",        "fields": ["tracking_number"],                          "priority": 5},
        {"name": "UTR Only",             "fields": ["utr_number"],                               "priority": 6},
    ],
    # AePS Cashout — Fingpay report vs Simplibank dump
    # Both TID and TrackingNumber (RRN) must match for a valid AePS match.
    # No UTR available for AePS transactions.
    "aeps": [
        {"name": "TID + Tracking", "fields": ["eko_tid", "tracking_number"], "priority": 1},
        {"name": "TID Only",       "fields": ["eko_tid"],                    "priority": 2},
    ],
    # Accept Payment (PG) — PayU report vs Simplibank dump
    # Match on TID + gross Amount. No tracking number in PayU data.
    "pg": [
        {"name": "TID + Amount", "fields": ["eko_tid", "amount"], "priority": 1},
        {"name": "TID Only",     "fields": ["eko_tid"],           "priority": 2},
    ],
    # Axis Bank DMT — Axis bank statement vs Simplibank Axis dump
    # IMPS: tracking_number parsed from Particulars; NEFT: eko_tid parsed from Particulars
    "axis": [
        {"name": "TID + Tracking", "fields": ["eko_tid", "tracking_number"], "priority": 1},
        {"name": "TID Only",       "fields": ["eko_tid"],                    "priority": 2},
        {"name": "Tracking Only",  "fields": ["tracking_number"],            "priority": 3},
    ],
    # Levin DMT — Levin bank statement vs Simplibank Levin dump.
    # Finance ops (Rajendra, 2026-07-16): tracking number / UTR are the PRIORITY match
    # keys for Levin, tried before the Eko TID.
    "levin": [
        {"name": "Tracking Only",  "fields": ["tracking_number"],            "priority": 1},
        {"name": "UTR Only",       "fields": ["utr_number"],                 "priority": 2},
        {"name": "TID + Tracking", "fields": ["eko_tid", "tracking_number"], "priority": 3},
        {"name": "TID Only",       "fields": ["eko_tid"],                    "priority": 4},
    ],
    # QR Collection — Paypoint transaction export vs Simplibank dump
    # Bank OrderId = internal TrackingNumber (primary)
    # Bank RRN     = internal UTRNUMBER (secondary)
    # All QR txns are Success on internal side; Failed bank rows auto-set to failed.
    "qr": [
        {"name": "TID + RRN",    "fields": ["eko_tid", "tracking_number"], "priority": 1},
        {"name": "RRN + Amount", "fields": ["tracking_number", "amount"],  "priority": 2},
        {"name": "TID Only",     "fields": ["eko_tid"],                    "priority": 3},
    ],
    # Digikhata PPI Wallet Load — Digikhata bank report vs Simplibank dump
    # The Simplibank dump has multiple sub-entries per TID (main load + CCF + GST + commission).
    # TID + Amount ensures we match the correct main load row and leave charge sub-entries visible.
    # Irrespective of status (Success / Fail / Refunded).
    "digikhata": [
        {"name": "TID + Tracking", "fields": ["eko_tid", "tracking_number"], "priority": 1},
        {"name": "TID + Amount",   "fields": ["eko_tid", "amount"],          "priority": 2},
        {"name": "TID Only",       "fields": ["eko_tid"],                    "priority": 3},
    ],
    # Indonepal Fund Transfer — Indonepal bank report vs Simplibank dump.
    # Eko TID is the PRIORITY match key (PartnerPinNo in bank = eko_trxn_id in dump).
    # (The old 'kiosk' key here was a mislabelled copy — SBI Kiosk uses its own engine and
    # never reads these rules, so it was inert; removed.)
    "indonepal": [
        {"name": "TID + Tracking", "fields": ["eko_tid", "tracking_number"], "priority": 1},
        {"name": "TID + UTR",      "fields": ["eko_tid", "utr_number"],      "priority": 2},
        {"name": "TID Only",       "fields": ["eko_tid"],                    "priority": 3},
    ],
}


def get_rules_for_partner(partner: str, db: Session) -> List[Dict]:
    """Get active custom rules from DB, fall back to defaults if none exist."""
    db_rules = db.query(MatchRule).filter(
        MatchRule.partner.in_([partner, "all"]),
        MatchRule.is_active == True
    ).order_by(MatchRule.priority).all()

    if db_rules:
        return [{"name": r.name, "fields": json.loads(r.match_fields), "priority": r.priority} for r in db_rules]

    return DEFAULT_RULES.get(partner, DEFAULT_RULES["airtel"])


def _normalize(value) -> str:
    if value is None:
        return ""
    # Normalise floats: 700.0 → "700", 25800.0 → "25800" so amount keys compare correctly
    if isinstance(value, float):
        return str(int(value)) if value == int(value) else f"{value:.2f}"
    return str(value).strip().lower().replace(" ", "")


def _build_key(txn: Transaction, fields: List[str]) -> Optional[str]:
    """Build a composite match key from specified fields."""
    parts = []
    for field in fields:
        val = _normalize(getattr(txn, field, None))
        if not val:
            return None  # If any key field is missing, skip this rule
        parts.append(val)
    return "||".join(parts)


def _repair_orphaned_matches(partner: str, recon_date: str, db: Session) -> int:
    """
    Before running reconciliation, detect and reset any stale 'matched' rows
    whose matched_with_id points to a transaction that no longer exists.

    This handles the case where one side of a matched pair was deleted
    (e.g. a dump re-upload after clearing bad data) without going through
    the cascade-reset in the delete endpoint — for example, a manual DB
    delete or a clear that ran before the cascade logic existed.

    Safe to call on every reconciliation run — is a no-op when nothing is wrong.
    Returns the number of rows repaired.
    """
    # Every status that represents a LINKED PAIR — not just plain 'matched'. A stale
    # manual_matched / amount_mismatch / reversal_matched / internal_matched /
    # interbank_matched / duplicate row whose partner vanished is just as much a lie,
    # and previously never healed because only 'matched' was inspected.
    _PAIRED = ("matched", "manual_matched", "amount_mismatch", "reversal_matched",
               "internal_matched", "interbank_matched", "duplicate")
    repaired = 0
    for side in ("bank", "internal"):
        stale = db.query(Transaction).filter(
            Transaction.partner == partner,
            Transaction.recon_date == recon_date,
            Transaction.side == side,
            Transaction.recon_status.in_(_PAIRED),
            Transaction.matched_with_id.isnot(None),
        ).all()
        for row in stale:
            exists = db.query(Transaction.id).filter(
                Transaction.id == row.matched_with_id
            ).first()
            if not exists:
                row.recon_status = "unmatched"
                row.matched_with_id = None
                row.match_id = None
                repaired += 1
    if repaired:
        db.commit()
    return repaired


@_serialized
def run_reconciliation(partner: str, recon_date: str, db: Session, user_id: str) -> Dict:
    """
    Main reconciliation function.
    0. Self-heal: reset any orphaned matched rows for this (partner, date)
       so stale matches never block fresh reconciliation.
    1. Loads unmatched bank + internal transactions for the given partner/date
    2. Applies rules in priority order
    3. Marks matched pairs, records results
    4. Returns summary stats
    """
    # Step 0: repair orphaned matches before loading — no manual intervention needed
    _repair_orphaned_matches(partner, recon_date, db)

    # Load transactions
    bank_txns = db.query(Transaction).filter(
        Transaction.partner == partner,
        Transaction.side == "bank",
        Transaction.recon_date == recon_date,
        Transaction.recon_status == ReconStatus.unmatched
    ).all()

    internal_txns = db.query(Transaction).filter(
        Transaction.partner == partner,
        Transaction.side == "internal",
        Transaction.recon_date == recon_date,
        Transaction.recon_status == ReconStatus.unmatched
    ).all()

    if not bank_txns and not internal_txns:
        return {
            "matched": 0, "unmatched_bank": 0, "unmatched_internal": 0,
            "total_bank": 0, "total_internal": 0, "rules_applied": []
        }

    rules = get_rules_for_partner(partner, db)
    rules_applied = []
    matched_count = 0

    # Sequential match ID counter — fetch starting point once, increment in memory
    seq = _next_seq(partner, recon_date, db)

    # Track which transactions are already matched in this run
    matched_bank_ids = set()
    matched_internal_ids = set()

    for rule in rules:
        fields = rule["fields"]
        rule_name = rule["name"]

        # Build lookup map from internal transactions
        internal_map: Dict[str, Transaction] = {}
        for txn in internal_txns:
            if txn.id in matched_internal_ids:
                continue
            key = _build_key(txn, fields)
            if key and key not in internal_map:
                internal_map[key] = txn

        rule_matches = 0
        for bank_txn in bank_txns:
            if bank_txn.id in matched_bank_ids:
                continue
            key = _build_key(bank_txn, fields)
            if key and key in internal_map:
                internal_txn = internal_map.pop(key)

                # Detect amount mismatch (₹1 tolerance for float rounding)
                bank_amt     = bank_txn.amount or 0
                internal_amt = internal_txn.amount or 0
                amt_ok       = abs(bank_amt - internal_amt) <= 1.0

                # Mark both — share a sequential match_id
                mid         = _make_match_id(partner, recon_date, seq)
                seq        += 1
                final_status = ReconStatus.matched if amt_ok else ReconStatus.amount_mismatch
                bank_txn.recon_status    = final_status
                bank_txn.matched_with_id = internal_txn.id
                bank_txn.match_id        = mid
                internal_txn.recon_status    = final_status
                internal_txn.matched_with_id = bank_txn.id
                internal_txn.match_id        = mid

                matched_bank_ids.add(bank_txn.id)
                matched_internal_ids.add(internal_txn.id)
                rule_matches += 1
                matched_count += 1

        if rule_matches > 0:
            rules_applied.append(f"{rule_name}: {rule_matches} matches")

    # ── Post-processing: flag duplicates ─────────────────────────────────────
    #
    # A "duplicate" is any remaining UNMATCHED transaction whose key field(s)
    # already appear on the OTHER side in a successfully matched pair.
    #
    # Examples:
    #   Bank=1 row TID=X,  Internal=2 rows TID=X → internal row 2 = duplicate
    #   Bank=2 rows TID=X, Internal=1 row TID=X  → bank row 2 = duplicate
    #
    # We check eko_tid AND tracking_number independently so we catch
    # partial-key duplicates too.

    # Build sets of key field values from the MATCHED transactions (per side)
    def _field_set(txns, ids, field):
        return {_normalize(getattr(t, field, None))
                for t in txns if t.id in ids and getattr(t, field, None)}

    matched_bank_tids   = _field_set(bank_txns,     matched_bank_ids,     "eko_tid")
    matched_bank_track  = _field_set(bank_txns,     matched_bank_ids,     "tracking_number")
    matched_int_tids    = _field_set(internal_txns, matched_internal_ids, "eko_tid")
    matched_int_track   = _field_set(internal_txns, matched_internal_ids, "tracking_number")

    duplicate_count = 0

    # Internal txns whose TID/tracking matches something already matched on bank side
    for txn in internal_txns:
        if txn.id in matched_internal_ids:
            continue
        tid   = _normalize(txn.eko_tid)       if txn.eko_tid       else None
        track = _normalize(txn.tracking_number) if txn.tracking_number else None
        if (tid and tid in matched_bank_tids) or (track and track in matched_bank_track):
            txn.recon_status = ReconStatus.duplicate
            duplicate_count += 1

    # Bank txns whose TID/tracking matches something already matched on internal side
    for txn in bank_txns:
        if txn.id in matched_bank_ids:
            continue
        tid   = _normalize(txn.eko_tid)       if txn.eko_tid       else None
        track = _normalize(txn.tracking_number) if txn.tracking_number else None
        if (tid and tid in matched_int_tids) or (track and track in matched_int_track):
            txn.recon_status = ReconStatus.duplicate
            duplicate_count += 1

    db.commit()

    unmatched_bank      = len([t for t in bank_txns     if t.id not in matched_bank_ids     and t.recon_status != ReconStatus.duplicate])
    unmatched_internal  = len([t for t in internal_txns if t.id not in matched_internal_ids and t.recon_status != ReconStatus.duplicate])

    # Record run
    run = ReconRun(
        user_id=user_id,
        partner=partner,
        recon_date=recon_date,
        total_bank=len(bank_txns),
        total_internal=len(internal_txns),
        matched=matched_count,
        unmatched_bank=unmatched_bank,
        unmatched_internal=unmatched_internal,
        rules_applied=json.dumps(rules_applied + ([f"Duplicates flagged: {duplicate_count}"] if duplicate_count else []))
    )
    db.add(run)
    db.commit()

    return {
        "matched": matched_count,
        "unmatched_bank": unmatched_bank,
        "unmatched_internal": unmatched_internal,
        "duplicate": duplicate_count,
        "total_bank": len(bank_txns),
        "total_internal": len(internal_txns),
        "rules_applied": rules_applied,
        "run_id": run.id
    }


def run_neft_d1_match(partner: str, recon_date: str, db: Session, user_id: str) -> Dict:
    """
    NEFT D+1 matching: take unmatched bank-side transactions from yesterday
    and try to match them against today's internal transactions.
    """
    from datetime import datetime, timedelta
    prev_date = (datetime.strptime(recon_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

    # Get yesterday's unmatched bank NEFT transactions (those with UTR)
    bank_txns = db.query(Transaction).filter(
        Transaction.partner == partner,
        Transaction.side == "bank",
        Transaction.recon_date == prev_date,
        Transaction.recon_status == ReconStatus.unmatched,
        Transaction.utr_number != None
    ).all()

    # Get today's unmatched internal transactions
    internal_txns = db.query(Transaction).filter(
        Transaction.partner == partner,
        Transaction.side == "internal",
        Transaction.recon_date == recon_date,
        Transaction.recon_status == ReconStatus.unmatched
    ).all()

    matched_count = 0
    # Allocate the sequence ONCE and increment it in memory (mirrors
    # run_reconciliation). SessionLocal uses autoflush=False, so calling
    # _next_seq() per match would re-read the same un-flushed MAX(seq) and
    # reissue ONE duplicate match_id for every pair matched in this run.
    # Matching itself — UTR-only, behavior-contract #8 — is unchanged.
    seq = _next_seq(partner, recon_date, db)
    for bank_txn in bank_txns:
        for internal_txn in internal_txns:
            if internal_txn.recon_status == ReconStatus.matched:
                continue
            # Match on UTR + amount
            if (_normalize(bank_txn.utr_number) == _normalize(internal_txn.utr_number)
                    and bank_txn.utr_number):
                mid = _make_match_id(partner, recon_date, seq)
                seq += 1
                bank_txn.recon_status    = ReconStatus.matched
                bank_txn.matched_with_id = internal_txn.id
                bank_txn.match_id        = mid
                internal_txn.recon_status    = ReconStatus.matched
                internal_txn.matched_with_id = bank_txn.id
                internal_txn.match_id        = mid
                matched_count += 1
                break

    db.commit()
    return {"neft_d1_matched": matched_count, "prev_date": prev_date}


def run_cross_date_rrn_match(partner: str, db: Session, user_id: str) -> Dict:
    """
    Cross-date RRN matching (QR T+1): the bank credit lands on day D but the
    internal record carries D+1 (or further), so per-(partner, recon_date)
    matching never sees the pair. This pass matches STILL-UNMATCHED bank ↔
    internal txn rows on tracking_number (UPI RRN — the true transaction
    identity) + amount within the core ₹1 tolerance, ACROSS recon dates.

    Deliberately narrow: identifier-based only (an RRN is unique per UPI txn),
    both sides must already be unmatched, first-match-wins in date order, and
    match IDs use the BANK row's recon_date via the normal MAX+1 sequencing —
    same discipline as run_neft_d1_match (seq allocated once per date and
    incremented in memory; autoflush=False would otherwise reissue duplicates).
    """
    bank_txns = db.query(Transaction).filter(
        Transaction.partner == partner,
        Transaction.side == "bank",
        Transaction.row_type == "txn",
        Transaction.recon_status == ReconStatus.unmatched,
        Transaction.tracking_number != None,          # noqa: E711
        Transaction.recon_date.like("20%"),           # 'auto' sentinel rows excluded
    ).order_by(Transaction.recon_date.asc()).all()
    internal_txns = db.query(Transaction).filter(
        Transaction.partner == partner,
        Transaction.side == "internal",
        Transaction.row_type == "txn",
        Transaction.recon_status == ReconStatus.unmatched,
        Transaction.tracking_number != None,          # noqa: E711
        Transaction.recon_date.like("20%"),
    ).order_by(Transaction.recon_date.asc()).all()

    # A row whose SOURCE status is failed moved no money — never pair it, even if
    # it slipped through ingest still marked unmatched (pre-flagging data). Same
    # literal set as the ingest pipelines (status sets are duplicated by convention).
    _failed_src = {
        'failed', 'failure', 'fail', 'f', '0',
        'rejected', 'reversed', 'cancelled', 'canceled',
        'error', 'expired', 'declined', 'refunded', 'timeout',
    }

    def _src_failed(t):
        return (t.status or "").strip().lower() in _failed_src

    by_track: Dict[str, list] = {}
    for i in internal_txns:
        k = _normalize(i.tracking_number)
        if k and not _src_failed(i):
            by_track.setdefault(k, []).append(i)

    matched_count, used = 0, set()
    seqs: Dict[str, int] = {}                          # bank recon_date → next seq
    for b in bank_txns:
        k = _normalize(b.tracking_number)
        if not k or _src_failed(b):
            continue
        cand = None
        for i in by_track.get(k, []):
            if i.id in used:
                continue
            if abs(float(b.amount or 0) - float(i.amount or 0)) <= 1.0:
                cand = i
                break
        if cand is None:
            continue
        d = b.recon_date
        if d not in seqs:
            seqs[d] = _next_seq(partner, d, db)
        mid = _make_match_id(partner, d, seqs[d])
        seqs[d] += 1
        b.recon_status    = ReconStatus.matched
        b.matched_with_id = cand.id
        b.match_id        = mid
        cand.recon_status    = ReconStatus.matched
        cand.matched_with_id = b.id
        cand.match_id        = mid
        used.add(cand.id)
        matched_count += 1

    db.commit()
    return {"cross_date_rrn_matched": matched_count}


# Partners whose bank credit and internal record legitimately sit on different
# recon dates (T+1 settlement recording) — the only ones the cross-date pass
# runs for. Extend deliberately, never wholesale: other products match same-day
# and a date-free pass would change their behavior.
CROSS_DATE_RRN_PARTNERS = ("qr",)


def flag_same_side_duplicates(partner: str, side: str, db: Session) -> Dict:
    """
    Flag re-ingested duplicate rows as 'duplicate' so they drop out of Open Items
    while remaining in the ledger for audit.

    A duplicate is a row_type=='txn' row that shares its tracking_number with
    another 'txn' row on the SAME (partner, side) — i.e. the same transaction
    ingested more than once (overlapping / repeated uploads, or re-upload after a
    clear wiped the file-hash guard). Keeps exactly ONE row per tracking_number —
    the matched copy if any, else an operator-actioned copy, else the
    lexicographically-first id — and flags only the still-'unmatched' extras.

    Deliberately scoped, to never reclassify legitimate rows:
      * row_type == 'txn' ONLY — reversals / fee rows legitimately share the
        original's tracking number (behaviour-contract #18) and are left untouched.
      * flags ONLY rows still in the default 'unmatched' state — never a matched
        row, and never one an operator has already actioned (src_assigned,
        human_override, failed, duplicate, …).

    Non-destructive and idempotent.
    """
    from collections import defaultdict
    rows = db.query(Transaction).filter(
        Transaction.partner == partner,
        Transaction.side == side,
        Transaction.row_type == "txn",
        Transaction.tracking_number.isnot(None),
        Transaction.tracking_number != "",
    ).all()

    groups = defaultdict(list)
    for r in rows:
        groups[r.tracking_number].append(r)

    flagged = 0
    for grp in groups.values():
        if len(grp) < 2:
            continue
        # Choose the survivor: matched wins, then any operator-actioned row,
        # then the deterministic lexicographically-first id.
        keeper = next((g for g in grp if g.recon_status == ReconStatus.matched), None)
        if keeper is None:
            keeper = next((g for g in grp if g.recon_status not in
                           (ReconStatus.unmatched, ReconStatus.duplicate)), None)
        if keeper is None:
            keeper = sorted(grp, key=lambda g: g.id)[0]
        for g in grp:
            if g.id != keeper.id and g.recon_status == ReconStatus.unmatched:
                g.recon_status = ReconStatus.duplicate
                flagged += 1

    if flagged:
        db.commit()
    return {"duplicates_flagged": flagged}


def run_reversal_match(partner: str, recon_date: str, db: Session, user_id: str) -> Dict:
    """
    Bank-to-bank reversal matching.
    Pairs reversal rows with their original bank rows using tracking_number.

    Pairing is type-aware to avoid key collisions:
        "reversal"     → pairs with "txn"        (IMPS/NEFT reversal ↔ original debit)
        "fee_reversal" → pairs with "fee_charge"  (fee reversal ↔ original fee row)

    This matters because the fee charge for an IMPS transaction shares the SAME
    tracking number as the original IMPS row — mixing them in one dict would
    cause one pair to be silently dropped.

    Looks across ALL dates (no recon_date filter on originals) because reversals
    can arrive a day after the original transaction.

    Both sides get recon_status = reversal_matched + a shared match_id.
    """
    # Load unmatched reversal rows for this partner / date being uploaded
    reversal_rows = db.query(Transaction).filter(
        Transaction.partner == partner,
        Transaction.side == "bank",
        Transaction.recon_date == recon_date,
        Transaction.row_type.in_(["reversal", "fee_reversal"]),
        Transaction.recon_status == ReconStatus.unmatched,
        Transaction.tracking_number.isnot(None),
    ).all()

    if not reversal_rows:
        return {"reversal_matched": 0}

    def _build_list_map(rows, field: str = "tracking_number"):
        """Index rows as lists-per-field-value so duplicates aren't lost."""
        m: Dict[str, list] = {}
        for r in rows:
            key = _normalize(getattr(r, field, None) or "")
            if key:
                m.setdefault(key, []).append(r)
        return m

    # Separate reversal types — they pair with different original row_types
    txn_reversals  = [r for r in reversal_rows if r.row_type == "reversal"]
    fee_reversals  = [r for r in reversal_rows if r.row_type == "fee_reversal"]

    txn_rev_map  = _build_list_map(txn_reversals)
    fee_rev_map  = _build_list_map(fee_reversals)

    # Collect all tracking keys across both reversal types for the query
    all_rev_trackings = set(txn_rev_map.keys()) | set(fee_rev_map.keys())
    if not all_rev_trackings:
        return {"reversal_matched": 0}

    # Load candidate originals for txn reversals — two strategies:
    #   1. Fino-style: reversal tracking == original tracking (same IMPS RRN)
    #   2. Airtel-style: reversal tracking == original utr_number (= APB_TXN_id)
    txn_originals = db.query(Transaction).filter(
        Transaction.partner == partner,
        Transaction.side == "bank",
        Transaction.row_type == "txn",
    ).filter(
        # Match if tracking_number OR utr_number equals any reversal tracking key
        Transaction.tracking_number.in_(list(all_rev_trackings)) |
        Transaction.utr_number.in_(list(all_rev_trackings))
    ).all()

    fee_originals = db.query(Transaction).filter(
        Transaction.partner == partner,
        Transaction.side == "bank",
        Transaction.row_type == "fee_charge",
        Transaction.tracking_number.in_(list(fee_rev_map.keys())),
    ).all()

    # Build two lookup maps for txn originals:
    #   by_tracking — Fino: reversal.tracking == original.tracking
    #   by_utr      — Airtel: reversal.tracking == original.utr_number (= APB_TXN_id)
    txn_orig_by_tracking = _build_list_map(txn_originals, "tracking_number")
    txn_orig_by_utr      = _build_list_map(txn_originals, "utr_number")
    fee_orig_by_tracking = _build_list_map(fee_originals, "tracking_number")

    matched_count = 0
    seq = _next_seq(partner, recon_date, db)
    already_matched_orig_ids: set = set()

    def _do_pair(rev_txn, orig_txn):
        nonlocal matched_count, seq
        if orig_txn.id in already_matched_orig_ids:
            return False
        mid = _make_match_id(partner, recon_date, seq)
        seq += 1
        rev_txn.recon_status    = ReconStatus.reversal_matched
        rev_txn.matched_with_id = orig_txn.id
        rev_txn.match_id        = mid
        orig_txn.recon_status    = ReconStatus.reversal_matched
        orig_txn.matched_with_id = rev_txn.id
        orig_txn.match_id        = mid
        already_matched_orig_ids.add(orig_txn.id)
        matched_count += 1
        return True

    # ── Pair txn reversals ────────────────────────────────────────────────────
    for tracking_key, reversals in txn_rev_map.items():
        # Try Fino-style first (tracking→tracking), then Airtel-style (tracking→utr)
        originals = txn_orig_by_tracking.get(tracking_key) or txn_orig_by_utr.get(tracking_key, [])
        for rev_txn, orig_txn in zip(reversals, originals):
            _do_pair(rev_txn, orig_txn)

    # ── Pair fee reversals ────────────────────────────────────────────────────
    for tracking_key, reversals in fee_rev_map.items():
        originals = fee_orig_by_tracking.get(tracking_key, [])
        for rev_txn, orig_txn in zip(reversals, originals):
            _do_pair(rev_txn, orig_txn)

    db.commit()
    return {"reversal_matched": matched_count}


def run_internal_match(partner: str, recon_date: str, db: Session, user_id: str) -> Dict:
    """
    Internal-to-Internal matching.

    Within the same Simplibank dump (partner + recon_date), a Success transaction
    and a Refunded/Failed transaction for the same eko_tid cancel each other out.
    Both rows are marked internal_matched and linked via a shared match_id.

    Logic:
    1. Load all internal rows for this partner + date
    2. Group by eko_tid
    3. For each TID with both a non-failed row AND a failed row:
       - Pair them (first-come-first-served if multiples)
       - Mark both internal_matched
    4. Return count of pairs created
    """
    failed_status_set = {
        'failed', 'failure', 'fail', 'f', '0',
        'rejected', 'reversed', 'cancelled', 'canceled',
        'error', 'expired', 'declined', 'refunded', 'timeout',
    }

    # Only operate on still-open rows so an internal pair can never steal a row
    # that already matched the bank (safe to run automatically after auto-recon).
    _OPEN = [ReconStatus.unmatched, ReconStatus.failed, ReconStatus.src_assigned]

    all_internal = db.query(Transaction).filter(
        Transaction.partner == partner,
        Transaction.side == "internal",
        Transaction.recon_date == recon_date,
        Transaction.row_type == "txn",
        Transaction.recon_status.in_(_OPEN),
        Transaction.eko_tid.isnot(None),
    ).all()

    if not all_internal:
        return {"internal_matched": 0}

    # Group by normalised TID
    by_tid: Dict[str, list] = {}
    for t in all_internal:
        key = _normalize(t.eko_tid)
        if key:
            by_tid.setdefault(key, []).append(t)

    seq = _next_seq(partner + "_INT", recon_date, db)
    matched_count = 0

    for tid_key, txns in by_tid.items():
        if len(txns) < 2:
            continue

        # Separate into success-side and failed-side
        success_rows = [t for t in txns
                        if t.recon_status not in (ReconStatus.internal_matched,)
                        and (t.status or "").lower() not in failed_status_set]
        failed_rows  = [t for t in txns
                        if t.recon_status not in (ReconStatus.internal_matched,)
                        and (t.status or "").lower() in failed_status_set]

        for orig, refund in zip(success_rows, failed_rows):
            mid = _make_match_id(partner + "I", recon_date, seq)
            seq += 1
            for row in (orig, refund):
                row.recon_status    = ReconStatus.internal_matched
                row.matched_with_id = (refund if row is orig else orig).id
                row.match_id        = mid
            matched_count += 1

    # ── Pass 2: DR/CR contra pairs by tracking (fallback TID) ──────────────────
    # A debit and its reversing credit inside the same dump cancel out even when
    # they carry DIFFERENT TIDs — they share a TrackingNumber. Pair any two still-
    # open internal rows with the same tracking (or TID when tracking is blank),
    # equal amount (₹1 tolerance) and opposite DR/CR. Status-agnostic, so it
    # covers refund reversals AND ordinary contra entries.
    contra_rows = db.query(Transaction).filter(
        Transaction.partner == partner,
        Transaction.side == "internal",
        Transaction.recon_date == recon_date,
        Transaction.row_type == "txn",
        Transaction.recon_status.in_(_OPEN),
        Transaction.dr_cr.isnot(None),
    ).all()

    # NULL placeholders (\N, NULL, None, NA…) must NOT become a grouping key —
    # otherwise every row with a null tracking collapses into one bucket and
    # unrelated TIDs get amount-matched. Treat them as blank → fall back to TID.
    _NULLK = {"", "\\n", "null", "none", "na", "n/a"}
    by_key: Dict[str, list] = {}
    for t in contra_rows:
        if t.recon_status == ReconStatus.internal_matched:
            continue   # already paired in pass 1
        key = _normalize(t.tracking_number or "")
        if key in _NULLK:
            key = _normalize(t.eko_tid or "")   # tracking is null → key on TID
        if key and key not in _NULLK:
            by_key.setdefault(key, []).append(t)

    for key, rows in by_key.items():
        debits  = [r for r in rows if (r.dr_cr or "").strip().upper() == "DR"]
        credits = [r for r in rows if (r.dr_cr or "").strip().upper() == "CR"]
        used_credit: set = set()
        for dr in debits:
            for cr in credits:
                if cr.id in used_credit:
                    continue
                if abs((dr.amount or 0) - (cr.amount or 0)) <= 1.0:
                    mid = _make_match_id(partner + "I", recon_date, seq)
                    seq += 1
                    for row, other in ((dr, cr), (cr, dr)):
                        row.recon_status    = ReconStatus.internal_matched
                        row.matched_with_id = other.id
                        row.match_id        = mid
                    used_credit.add(cr.id)
                    matched_count += 1
                    break

    if matched_count:
        db.commit()

    return {"internal_matched": matched_count}


def assign_src(transaction_id: str, src_code: str, src_note: str, db: Session) -> Transaction:
    txn = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not txn:
        raise ValueError(f"Transaction {transaction_id} not found")
    txn.src_code = src_code
    txn.src_note = src_note
    txn.recon_status = ReconStatus.src_assigned
    db.commit()
    return txn


def manual_match(bank_txn_id: str, internal_txn_id: str, db: Session) -> Dict:
    bank_txn = db.query(Transaction).filter(Transaction.id == bank_txn_id).first()
    internal_txn = db.query(Transaction).filter(Transaction.id == internal_txn_id).first()
    if not bank_txn or not internal_txn:
        raise ValueError("One or both transactions not found")
    p = bank_txn.partner or "manual"
    d = bank_txn.recon_date or ""
    seq = _next_seq(p, d, db)
    mid = _make_match_id(p, d, seq)
    # Amount guard — parity with the auto-matcher (run_reconciliation): within ±₹1 it's
    # a clean match; beyond that the pair is still LINKED (shared match_id) but flagged
    # `amount_mismatch` so a real ₹ gap can never hide behind a "Manual Matched" label.
    amt_diff = abs(float(bank_txn.amount or 0) - float(internal_txn.amount or 0))
    is_mismatch = amt_diff > 1.0
    final_status = ReconStatus.amount_mismatch if is_mismatch else ReconStatus.manual_matched
    bank_txn.recon_status    = final_status
    bank_txn.matched_with_id = internal_txn_id
    bank_txn.match_id        = mid
    internal_txn.recon_status    = final_status
    internal_txn.matched_with_id = bank_txn_id
    internal_txn.match_id        = mid
    _record_rule_suggestion(db, bank_txn, internal_txn)   # Tier 2 rule learning
    db.commit()
    return {"status": final_status.value, "bank_id": bank_txn_id, "internal_id": internal_txn_id,
            "match_id": mid, "amount_diff": round(amt_diff, 2), "amount_mismatch": is_mismatch}


def _record_rule_suggestion(db: Session, bank_txn, internal_txn):
    """
    Tier 2 rule learning: infer which fields actually matched in this manual
    pair and bump the (partner, field-set) pattern counter. Repeated patterns
    surface in Workflow → Rule Suggestions as candidate MatchRules.
    Never raises — learning must not break the match itself.
    """
    try:
        from models.database import RuleSuggestion
        norm = lambda x: str(x or "").strip().upper()  # noqa: E731
        fields = []
        if norm(bank_txn.eko_tid) and norm(bank_txn.eko_tid) == norm(internal_txn.eko_tid):
            fields.append("eko_tid")
        if norm(bank_txn.tracking_number) and norm(bank_txn.tracking_number) == norm(internal_txn.tracking_number):
            fields.append("tracking_number")
        if norm(bank_txn.utr_number) and norm(bank_txn.utr_number) == norm(internal_txn.utr_number):
            fields.append("utr_number")
        if not fields:
            try:
                if abs(float(bank_txn.amount or 0) - float(internal_txn.amount or 0)) <= 0.01:
                    fields.append("amount")
            except (TypeError, ValueError):
                pass
        if not fields:
            return
        partner = bank_txn.partner or "unknown"
        csv = ",".join(fields)
        row = db.query(RuleSuggestion).filter(
            RuleSuggestion.partner == partner, RuleSuggestion.fields_csv == csv).first()
        if row:
            row.hit_count = (row.hit_count or 0) + 1
            row.last_seen = datetime.datetime.utcnow()
            if row.status == "dismissed":
                pass  # stay dismissed — user said no
        else:
            db.add(RuleSuggestion(partner=partner, fields_csv=csv))
    except Exception:
        pass


# ── Interbank Fund Movement Matching ─────────────────────────────────────────

def run_interbank_match(db: Session, user_id: str) -> Dict:
    """
    Cross-bank UTR matching for internal fund transfers.

    Logic:
    - Find all bank-side transactions with recon_status = 'fund_transfer' AND a UTR number.
      These are CR entries (money received from another EKO bank account).
    - Find all bank-side transactions with recon_status = 'fund_transfer' AND a UTR number
      that are DR entries (money sent out — fund transfer debits).
    - Match CR ↔ DR pairs that share the same UTR number AND the amounts are equal.
    - Both sides get recon_status = 'interbank_matched' and a shared match_id (IBT- prefix).
    - Cross-date and cross-partner: any bank/any date can match any other bank/any date.

    Returns summary of matched, unmatched CR, unmatched DR counts.
    """
    # Load all fund_transfer rows with a UTR
    fund_rows = db.query(Transaction).filter(
        Transaction.recon_status == ReconStatus.fund_transfer,
        Transaction.utr_number.isnot(None),
        Transaction.utr_number != '',
        Transaction.side == "bank",
    ).all()

    # Separate into CR (received) and DR (sent) using only dr_cr + amount
    from collections import defaultdict
    cr_by_utr = defaultdict(list)
    dr_by_utr = defaultdict(list)
    for r in fund_rows:
        is_cr = (r.dr_cr or '').upper() == 'CR'
        if is_cr:
            cr_by_utr[r.utr_number].append(r)
        else:
            dr_by_utr[r.utr_number].append(r)

    matched = 0
    used_cr = set()
    used_dr = set()

    for utr, cr_list in cr_by_utr.items():
        dr_list = dr_by_utr.get(utr, [])
        for cr in cr_list:
            if cr.id in used_cr:
                continue
            for dr in dr_list:
                if dr.id in used_dr:
                    continue
                # Amount must match (within ₹1 tolerance)
                cr_amt = abs(cr.amount or cr.credit or 0)
                dr_amt = abs(dr.amount or dr.debit or 0)
                if abs(cr_amt - dr_amt) > 1.0:
                    continue

                # Build match ID with IBT prefix
                seq = _next_seq("interbank", cr.recon_date or "", db)
                mid = f"IBT-{(cr.recon_date or '').replace('-','')[:8]}-{seq:04d}"

                cr.recon_status    = ReconStatus.interbank_matched
                cr.match_id        = mid
                cr.matched_with_id = dr.id
                dr.recon_status    = ReconStatus.interbank_matched
                dr.match_id        = mid
                dr.matched_with_id = cr.id

                used_cr.add(cr.id)
                used_dr.add(dr.id)
                matched += 1
                break

    db.commit()

    unmatched_cr = sum(1 for r in fund_rows if r.id not in used_cr
                       and (r.dr_cr or '').upper() == 'CR')
    unmatched_dr = sum(1 for r in fund_rows if r.id not in used_dr
                       and (r.dr_cr or '').upper() == 'DR')

    from models.database import AuditLog, generate_id
    import json, datetime
    try:
        db.add(AuditLog(
            id=generate_id(), user_id=user_id, username="system",
            action="interbank_match", action_type="app",
            entity_type="transaction",
            detail=json.dumps({"matched": matched, "unmatched_cr": unmatched_cr,
                               "unmatched_dr": unmatched_dr}),
        ))
        db.commit()
    except Exception:
        db.rollback()

    return {
        "interbank_matched": matched,
        "unmatched_cr":      unmatched_cr,
        "unmatched_dr":      unmatched_dr,
    }
