"""
core/ai_anomalies.py — AI anomaly detection (advisory, human-reviewed).

The rule scan (/workflow/anomalies) can only express exact GROUP BY equality —
duplicate UTRs/TIDs and volume spikes. This engine gives Claude the SAME
DE-IDENTIFIED rows and asks for the fuzzy/structural anomalies SQL cannot see:
near-duplicate references (truncation/transposition/edit-distance — e.g. the
truncated-KO class), same-amount/different-ref clusters, timing outliers, and
suspicious round-number or velocity patterns.

Advisory only: every finding is reviewed by a human, who acts through the
existing confirmed paths. The AI never writes. Same guarantees as core.ai_matcher
(de-identified payload, opaque tokens, graceful degradation, never raises).
"""
import json
import logging

from core import ai_client
from core.ai_deident import deidentify_rows, assert_no_pii

logger = logging.getLogger("eko_recon.ai_anomalies")

MAX_ROWS = 150   # cap rows analysed per scan → bounds token cost / latency

_SYSTEM = (
    "You are a reconciliation risk analyst for an Indian fintech. You are given DE-IDENTIFIED "
    "ledger rows (only a ref token, amount, date, DR/CR, status, and reference numbers "
    "tid/track/utr). Find ANOMALIES a simple duplicate/volume SQL scan would miss.\n"
    "Look for:\n"
    "  • near-duplicate references — same underlying id truncated, transposed, or off by a "
    "character between rows (a common bank-truncation defect);\n"
    "  • same-amount / same-day clusters with different references (possible double-payment "
    "or split);\n"
    "  • timing outliers — bursts, or a reference dated far from its peers;\n"
    "  • suspicious round-number or repeated-amount patterns.\n"
    "Rules:\n"
    "  • Refer to rows ONLY by their ref tokens, exactly as given. Never invent a token.\n"
    "  • Each finding: a kind, a severity (high/medium/low), a one-line title, a short detail, "
    "and the ref tokens involved.\n"
    "  • Only report genuine anomalies. If nothing stands out, return an empty list."
)

_TOOL = {
    "name": "report_anomalies",
    "description": "Report the anomalies found among the de-identified rows.",
    "input_schema": {
        "type": "object",
        "properties": {
            "anomalies": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind":     {"type": "string", "description": "e.g. near_duplicate, same_amount_diff_ref, timing_outlier, round_number"},
                        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                        "title":    {"type": "string", "description": "one-line human label"},
                        "detail":   {"type": "string", "description": "short reason (reference tokens/amounts only, no names)"},
                        "refs":     {"type": "array", "items": {"type": "string"}, "description": "ref tokens involved, e.g. ['T3','T18']"},
                    },
                    "required": ["kind", "severity", "title", "refs"],
                },
            }
        },
        "required": ["anomalies"],
    },
}

_SEV_RANK = {"high": 3, "medium": 2, "low": 1}


def _call_model(payload):
    """Isolated network boundary (monkeypatched in tests)."""
    user = (
        "Rows:\n" + json.dumps(payload, separators=(",", ":")) +
        "\n\nCall report_anomalies with any anomalies you find."
    )
    data = ai_client.call_tool(_SYSTEM, user, _TOOL)
    return data.get("anomalies", []) or []


def detect_anomalies(rows, *, limit=50, max_rows=MAX_ROWS):
    """
    Return {"ai_anomalies": [...], "source": "ai", "scanned": n, "error"?: str, "note"?: str}.
    Each anomaly: {kind, severity, title, detail, row_ids:[...], amount, date, partner, source:"ai"}.
    Never raises.
    """
    rows = list(rows)
    total = len(rows)
    if not ai_client.is_enabled():
        return {"ai_anomalies": [], "source": "ai", "scanned": 0,
                "error": "AI is not configured. Set the Anthropic key in Configuration → AI."}
    if not rows:
        return {"ai_anomalies": [], "source": "ai", "scanned": 0}

    truncated = total > max_rows
    rows = rows[:max_rows]

    payload, token_map = deidentify_rows(rows, "T")
    try:
        assert_no_pii(payload, rows)
    except ValueError as e:
        logger.error("ai_anomalies aborted (privacy guard): %s", e)
        return {"ai_anomalies": [], "source": "ai", "scanned": 0,
                "error": "AI anomaly scan blocked by the privacy guard."}

    try:
        found = _call_model(payload)
    except Exception as e:
        logger.warning("ai_anomalies model call failed: %s", e)
        return {"ai_anomalies": [], "source": "ai", "scanned": len(rows),
                "error": "AI anomaly service is unavailable right now."}

    by_id = {r.id: r for r in rows}
    out = []
    for a in found:
        if not isinstance(a, dict):
            continue
        # Resolve ref tokens back to real ids; reject anything the model invented.
        ref_ids = [token_map.get(str(t)) for t in (a.get("refs") or [])]
        ref_ids = [rid for rid in ref_ids if rid]
        if not ref_ids:
            continue
        rep = by_id.get(ref_ids[0])
        sev = str(a.get("severity") or "low").lower()
        if sev not in _SEV_RANK:
            sev = "low"
        out.append({
            "source": "ai",
            "kind": str(a.get("kind") or "anomaly").strip()[:40],
            "severity": sev,
            "title": str(a.get("title") or "Anomaly").strip()[:200],
            "detail": str(a.get("detail") or "").strip()[:500],
            "row_ids": ref_ids,
            "amount": getattr(rep, "amount", None) if rep else None,
            "date": (getattr(rep, "recon_date", None) if rep else None),
            "partner": getattr(rep, "partner", None) if rep else None,
        })

    out.sort(key=lambda x: -_SEV_RANK.get(x["severity"], 0))
    result = {"ai_anomalies": out[:limit], "source": "ai", "scanned": len(rows)}
    if truncated:
        result["note"] = f"Analysed the first {max_rows} rows; narrow the filter for the rest."
    return result
