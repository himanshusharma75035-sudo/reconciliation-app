"""
core/ai_rules.py — AI-proposed matching rules (advisory, human-promoted).

The existing Tier-2 learning surfaces rules that *already worked* in manual
matches. This engine looks at the DE-IDENTIFIED unmatched rows for a partner and
proposes NEW rule field-combinations that would likely pair more of them — e.g.
"these rows share a UTR but no active rule keys on UTR". It is advisory: a human
with logic_builder rights promotes a suggestion to a real MatchRule (same append-
at-lowest-priority path the learned suggestions use). The AI never writes.

Guarantees mirror core.ai_matcher: de-identified payload only, opaque tokens,
graceful degradation, never raises. Proposed fields are constrained to the match
vocabulary the engine actually understands, so a promoted rule is always valid.
"""
import json
import logging

from core import ai_client
from core.ai_deident import deidentify_rows, assert_no_pii

logger = logging.getLogger("eko_recon.ai_rules")

# The ONLY fields a proposed rule may use — these map 1:1 to Transaction columns
# the matcher keys on (getattr(txn, field)). Anything else would create a dead rule.
ALLOWED_FIELDS = ("eko_tid", "tracking_number", "utr_number", "amount")

MAX_SIDE = 120
MIN_CONFIDENCE = 55

_SYSTEM = (
    "You are a reconciliation engineer for an Indian fintech. You are shown DE-IDENTIFIED "
    "unmatched bank and internal ledger rows for one partner, plus the field-combinations "
    "that existing match rules already use. Propose NEW matching rules — each a combination "
    "of allowed fields — that would pair more of these rows without creating false matches.\n"
    "Allowed fields ONLY: eko_tid, tracking_number, utr_number, amount.\n"
    "Guidance:\n"
    "  • A good rule keys on fields that are present on BOTH sides and identify the same "
    "transaction (a shared UTR is strongest; amount alone is far too weak — never propose "
    "[amount] by itself).\n"
    "  • Do not repeat a field-combination that is already an existing rule.\n"
    "  • Prefer 1-2 field combinations. Give a short rationale grounded in the rows you saw "
    "(e.g. 'most pairs share utr_number but no active rule uses it') and a confidence 0-100.\n"
    "  • If nothing new is worth proposing, return an empty list."
)

_TOOL = {
    "name": "propose_rules",
    "description": "Propose new matching-rule field combinations for this partner.",
    "input_schema": {
        "type": "object",
        "properties": {
            "rules": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "fields": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(ALLOWED_FIELDS)},
                            "description": "the field-combination this rule matches on",
                        },
                        "rationale": {"type": "string", "description": "why, grounded in the rows"},
                        "confidence": {"type": "integer", "description": "0-100"},
                    },
                    "required": ["fields", "confidence"],
                },
            }
        },
        "required": ["rules"],
    },
}


def suggest_rules(bank_rows, internal_rows, partner, existing_field_sets, *, max_side=MAX_SIDE):
    """
    Return {"suggestions": [...], "error"?: str}. Each suggestion:
      {"source":"ai","partner":..,"fields":[...],"rationale":..,"confidence":int}
    `existing_field_sets` is an iterable of tuples/lists of field names already used
    by active rules (so we don't re-propose them). Never raises.
    """
    if not ai_client.is_enabled():
        return {"suggestions": [], "error": "AI is not configured. Set the Anthropic key in Configuration → AI."}

    bank_rows = list(bank_rows)[:max_side]
    internal_rows = list(internal_rows)[:max_side]
    if not bank_rows and not internal_rows:
        return {"suggestions": []}

    b_payload, _ = deidentify_rows(bank_rows, "B")
    i_payload, _ = deidentify_rows(internal_rows, "I")
    try:
        assert_no_pii(b_payload, bank_rows)
        assert_no_pii(i_payload, internal_rows)
    except ValueError as e:
        logger.error("ai_rules aborted (privacy guard): %s", e)
        return {"suggestions": [], "error": "AI rule analysis blocked by the privacy guard."}

    existing = {tuple(sorted(fs)) for fs in (existing_field_sets or [])}
    user = (
        f"Partner: {partner}\n"
        f"Existing rule field-sets: {json.dumps([sorted(list(fs)) for fs in existing])}\n\n"
        "BANK unmatched rows:\n" + json.dumps(b_payload, separators=(",", ":")) +
        "\n\nINTERNAL unmatched rows:\n" + json.dumps(i_payload, separators=(",", ":")) +
        "\n\nCall propose_rules."
    )
    try:
        data = ai_client.call_tool(_SYSTEM, user, _TOOL)
    except Exception as e:
        logger.warning("ai_rules model call failed: %s", e)
        return {"suggestions": [], "error": "AI rule service is unavailable right now."}

    out, seen = [], set(existing)
    for r in (data.get("rules") or []):
        if not isinstance(r, dict):
            continue
        # Keep only allowed fields, de-duplicated, order-normalised.
        fields = [f for f in (r.get("fields") or []) if f in ALLOWED_FIELDS]
        fields = list(dict.fromkeys(fields))  # dedupe, keep order
        if not fields or fields == ["amount"]:   # amount-only is never a valid rule
            continue
        key = tuple(sorted(fields))
        if key in seen:
            continue
        try:
            conf = int(r.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0
        if conf < MIN_CONFIDENCE:
            continue
        seen.add(key)
        out.append({
            "source": "ai",
            "partner": partner,
            "fields": fields,
            "rationale": str(r.get("rationale") or "").strip() or "AI-proposed rule",
            "confidence": max(0, min(conf, 100)),
        })
    out.sort(key=lambda s: -s["confidence"])
    return {"suggestions": out}
