"""
core/ai_matcher.py — AI-assisted match suggestions (advisory, human-approved).

Given the unmatched bank and internal rows for a partner/date-range, this asks
Claude to propose likely bank↔internal pairs. It is ADVISORY ONLY:

  • The model sees a DE-IDENTIFIED, allowlist-only payload (core.ai_deident) —
    no account numbers, no customer/retailer names, no narration, no raw rows.
  • The model refers to rows only by opaque tokens (B1, I7…) it cannot resolve.
  • This module maps the tokens back to real ids and returns suggestions in the
    EXACT same shape as the heuristic /workflow/suggested-matches, tagged
    "source": "ai" so the UI can label System vs AI.
  • It NEVER changes data. A human confirms every match via /recon/manual-match.
  • It never raises to the caller — on any failure it returns
    {"suggestions": [], "error": "..."} so the window degrades gracefully.

Provider-agnostic via core.ai_client — Anthropic, OpenAI, or any OpenAI-compatible / open-source
endpoint, configured in Configuration → AI. Model + key resolution live entirely in ai_client.
"""
import json
import logging

from core import ai_client
from core.ai_deident import deidentify_rows, assert_no_pii

logger = logging.getLogger("eko_recon.ai_matcher")

# Bound token cost + latency: never send more than this many rows per side.
MAX_SIDE = 80
# Only surface pairs the model is at least this sure about.
MIN_CONFIDENCE = 55

_SYSTEM = (
    "You are a reconciliation analyst for an Indian fintech. You are given two "
    "lists of UNMATCHED ledger rows — bank-statement rows and internal-system "
    "rows — that a rule engine could not pair. Each row is de-identified: you see "
    "only a ref token, amount, date, DR/CR, status, and transaction reference "
    "numbers (tid/track/utr). Propose the pairs that are the SAME underlying "
    "transaction.\n"
    "Rules:\n"
    "  • Pair only when it is genuinely likely to be the same money movement: "
    "amounts equal or within about ₹1, dates within a couple of days, and a "
    "shared or overlapping reference (utr/tid/track) whenever references exist.\n"
    "  • A UTR match is the strongest signal. Do not pair on amount+date alone "
    "unless there is no reference on either side and the match is otherwise "
    "unambiguous — use a lower confidence then.\n"
    "  • Each bank row pairs with at most one internal row, and vice-versa.\n"
    "  • Only use ref tokens exactly as given. Never invent a token.\n"
    "  • Return confidence 0–100 and a short reason. Omit weak/uncertain pairs."
)

_TOOL = {
    "name": "propose_matches",
    "description": "Return the bank↔internal row pairs that are the same underlying transaction.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pairs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "bank":       {"type": "string",  "description": "bank row ref token, e.g. B3"},
                        "internal":   {"type": "string",  "description": "internal row ref token, e.g. I7"},
                        "confidence": {"type": "integer", "description": "0-100 confidence this is the same transaction"},
                        "reason":     {"type": "string",  "description": "short justification, e.g. 'same UTR, exact amount'"},
                    },
                    "required": ["bank", "internal", "confidence"],
                },
            }
        },
        "required": ["pairs"],
    },
}


def _call_model(bank_payload, internal_payload):
    """Send the de-identified payloads to the configured model and return the raw pairs list.
    Provider-agnostic via core.ai_client (Anthropic, OpenAI, or any OpenAI-compatible / open-source
    endpoint). Isolated so tests can monkeypatch it without touching the network."""
    user = (
        "BANK rows (unmatched):\n" + json.dumps(bank_payload, separators=(",", ":")) +
        "\n\nINTERNAL rows (unmatched):\n" + json.dumps(internal_payload, separators=(",", ":")) +
        "\n\nCall propose_matches with the confident pairs."
    )
    data = ai_client.call_tool(_SYSTEM, user, _TOOL, max_tokens=4000)
    return (data or {}).get("pairs", []) or []


def _disp(r):
    """Display subset returned to the UI (same fields the heuristic endpoint sends)."""
    return {
        "id": getattr(r, "id", None),
        "eko_tid": getattr(r, "eko_tid", None),
        "tracking_number": getattr(r, "tracking_number", None),
        "amount": getattr(r, "amount", None),
        "transaction_date": getattr(r, "transaction_date", None),
        "recon_date": getattr(r, "recon_date", None),
    }


def suggest_matches(bank_rows, internal_rows, *, limit=50, max_side=MAX_SIDE):
    """
    Return {"suggestions": [...], "bank_unmatched": n, "internal_unmatched": m}.
    Each suggestion mirrors the heuristic endpoint plus "source": "ai":
      {"source":"ai","score":int,"why":[...],"bank":{...},"internal":{...}}
    Never raises; returns an "error" string on any failure.
    """
    bank_rows = list(bank_rows)
    internal_rows = list(internal_rows)
    total_bank, total_internal = len(bank_rows), len(internal_rows)

    if not ai_client.is_enabled():
        return {"suggestions": [], "bank_unmatched": total_bank, "internal_unmatched": total_internal,
                "error": "AI is not configured. Set a provider and key in Configuration → AI."}
    if not bank_rows or not internal_rows:
        return {"suggestions": [], "bank_unmatched": total_bank, "internal_unmatched": total_internal}

    # Cap batch size to bound token cost / latency.
    truncated = total_bank > max_side or total_internal > max_side
    bank_rows = bank_rows[:max_side]
    internal_rows = internal_rows[:max_side]

    b_payload, b_map = deidentify_rows(bank_rows, "B")
    i_payload, i_map = deidentify_rows(internal_rows, "I")

    # Fail-closed privacy guard — abort before any network call if PII leaked.
    try:
        assert_no_pii(b_payload, bank_rows)
        assert_no_pii(i_payload, internal_rows)
    except ValueError as e:
        logger.error("ai_matcher aborted (privacy guard): %s", e)
        return {"suggestions": [], "bank_unmatched": total_bank, "internal_unmatched": total_internal,
                "error": "AI suggestion blocked by the privacy guard."}

    try:
        pairs = _call_model(b_payload, i_payload)
    except Exception as e:
        logger.warning("ai_matcher model call failed: %s", e)
        return {"suggestions": [], "bank_unmatched": total_bank, "internal_unmatched": total_internal,
                "error": "AI suggestion service is unavailable right now."}

    by_id_bank = {r.id: r for r in bank_rows}
    by_id_internal = {r.id: r for r in internal_rows}

    suggestions, used_bank, used_internal = [], set(), set()
    for p in pairs:
        if not isinstance(p, dict):
            continue
        b_tok, i_tok = str(p.get("bank", "")), str(p.get("internal", ""))
        b_id, i_id = b_map.get(b_tok), i_map.get(i_tok)
        # Reject hallucinated / unknown tokens and 1-to-many reuse.
        if not b_id or not i_id:
            continue
        if b_id in used_bank or i_id in used_internal:
            continue
        try:
            score = int(p.get("confidence", 0))
        except (TypeError, ValueError):
            score = 0
        if score < MIN_CONFIDENCE:
            continue
        used_bank.add(b_id)
        used_internal.add(i_id)
        reason = str(p.get("reason") or "").strip()
        suggestions.append({
            "source": "ai",
            "score": max(0, min(score, 100)),
            "why": [reason] if reason else ["AI match"],
            "bank": _disp(by_id_bank[b_id]),
            "internal": _disp(by_id_internal[i_id]),
        })

    suggestions.sort(key=lambda s: -s["score"])
    out = {"suggestions": suggestions[:limit],
           "bank_unmatched": total_bank, "internal_unmatched": total_internal}
    if truncated:
        out["note"] = f"Analysed the first {max_side} rows per side; narrow the date range for the rest."
    return out
