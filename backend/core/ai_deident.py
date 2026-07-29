"""
core/ai_deident.py — the single de-identification choke-point for every AI call.

This app reconciles real money. Customer PII must never leave the process in an
AI request. This module is the ONLY place transaction rows are turned into an
AI-visible payload, and it is a strict ALLOWLIST: only match-relevant fields
(amounts, dates, DR/CR, transaction status, and transaction *reference* numbers —
TID / tracking / UTR) are emitted. Account numbers, customer & retailer names,
bank narration, the raw source row, and every override/assignment field are
dropped and never sent to the model.

Every row's real DB id is replaced by an opaque per-batch token ("B1", "I7"…).
The token→id map stays server-side, so the model can only ever refer to a row by
a token it cannot resolve to a customer, an account, or a name.

`assert_no_pii()` is a fail-closed guard run immediately before the payload is
handed to the model: if any sensitive value from the source rows is present in
the outgoing JSON, it raises and the AI call is aborted rather than leaking.
"""
import json
import logging

logger = logging.getLogger("eko_recon.ai_deident")

# The ONLY keys the model may ever see. Nothing outside this set is serialised.
SAFE_FIELDS = ("ref", "amount", "date", "drcr", "status", "tid", "track", "utr")

# Row attributes that are PII / sensitive and must never be emitted. Named
# explicitly so the intent is auditable and a regression test can assert it.
# (Includes a few defensive names that aren't on the model today but might be.)
PII_ATTRS = (
    "bank_account", "account_number", "account_no",
    "csp_name", "csp_code", "customer_name",
    "bank_description", "narration", "description",
    "src_note", "src_code", "raw_data",
    "override_note", "override_by", "assigned_to",
)


def _date10(*vals):
    """First non-empty value, trimmed to its YYYY-MM-DD prefix when long enough."""
    for v in vals:
        s = str(v or "").strip()
        if len(s) >= 10:
            return s[:10]
        if s:
            return s
    return ""


def deidentify_rows(rows, prefix):
    """
    Turn ORM Transaction rows into an allowlist-only, token-keyed payload.

    Returns (payload, token_map):
      • payload   — list of dicts whose keys ⊆ SAFE_FIELDS (None values dropped).
      • token_map — {token: real_id}. NEVER leaves the server.

    `prefix` is a short side tag, e.g. "B" (bank) or "I" (internal).
    """
    payload, token_map = [], {}
    for i, r in enumerate(rows, 1):
        tok = f"{prefix}{i}"
        token_map[tok] = getattr(r, "id", None)
        rec = {
            "ref":    tok,
            "amount": round(float(getattr(r, "amount", 0) or 0), 2),
            "date":   _date10(getattr(r, "recon_date", None), getattr(r, "transaction_date", None)),
            "drcr":   (getattr(r, "dr_cr", None) or "").strip().upper() or None,
            "status": (getattr(r, "status", None) or "").strip() or None,
            "tid":    (getattr(r, "eko_tid", None) or "").strip() or None,
            "track":  (getattr(r, "tracking_number", None) or "").strip() or None,
            "utr":    (getattr(r, "utr_number", None) or "").strip() or None,
        }
        payload.append({k: v for k, v in rec.items() if v is not None})
    return payload, token_map


def assert_no_pii(payload, rows):
    """
    Fail-closed guard: raise ValueError if any sensitive value from `rows` leaked
    into `payload`. Call this on the exact object about to be sent to the model.

    Only meaningful values (≥ 4 chars) are checked, so trivial tokens like "DR"
    or "0" never trip it. Erring toward blocking is deliberate — a false positive
    aborts one suggestion run; a false negative leaks a customer.
    """
    blob = json.dumps(payload, default=str).lower()
    for r in rows:
        for attr in PII_ATTRS:
            val = getattr(r, attr, None)
            if val is None:
                continue
            s = str(val).strip().lower()
            if len(s) >= 4 and s in blob:
                raise ValueError(
                    f"de-identification breach: '{attr}' value present in AI payload"
                )
    return True
