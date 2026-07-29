"""
core/ai_insights.py — AI narratives for the Insights tab (advisory, read-only).

Two entry points, both fed PURE AGGREGATES (counts, amounts, match-rates, ageing
buckets, product labels) — there is no customer PII in an aggregate, so no row
ever reaches the model. The functions build their own allowlisted aggregate dicts
and never accept raw rows, which keeps the "nothing but numbers and labels leaves
the process" guarantee obvious by construction.

  • narrate_trend(trend_payload)      → plain-English trend explanation + highlights
  • explain_escalation(stats)         → root-cause hypotheses + a ready escalation note

Both are advisory (a human reads/edits), tagged source="ai", and degrade
gracefully (no key / no credits / model error → empty result + an "error" string).
Nothing here writes.
"""
import json
import logging

from core import ai_client

logger = logging.getLogger("eko_recon.ai_insights")

_DISCLAIMER = "AI-generated from de-identified aggregates — advisory, verify before acting."


# ── 1) Trend narrative ────────────────────────────────────────────────────────

_TREND_SYSTEM = (
    "You are a reconciliation operations manager for an Indian fintech. You are given a table "
    "of per-product monthly reconciliation match-rates and open-item counts (aggregate numbers "
    "only — no customer data). Explain, in plain English for a finance lead, what the trend "
    "shows: which products are healthy, which are sliding, and where open items are piling up. "
    "Be specific with the numbers you were given. Do not invent products or figures."
)

_TREND_TOOL = {
    "name": "trend_summary",
    "description": "Summarise the reconciliation trend for a finance manager.",
    "input_schema": {
        "type": "object",
        "properties": {
            "narrative": {"type": "string", "description": "2-4 sentence plain-English summary"},
            "highlights": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "product": {"type": "string"},
                        "trend": {"type": "string", "enum": ["improving", "worsening", "flat"]},
                        "detail": {"type": "string", "description": "one line, cite the numbers"},
                    },
                    "required": ["product", "trend", "detail"],
                },
            },
        },
        "required": ["narrative"],
    },
}


def _trend_aggregate(trend_payload):
    """Allowlist the trend response into a compact numbers-only block for the model."""
    periods = list(trend_payload.get("periods") or [])
    rows = []
    for p in (trend_payload.get("partners") or []):
        by_month = [
            {"m": periods[i] if i < len(periods) else "",
             "rate": round(float(bm.get("match_rate", 0) or 0), 1),
             "open": int(bm.get("open", 0) or 0),
             "total": int(bm.get("total", 0) or 0)}
            for i, bm in enumerate(p.get("by_month") or [])
        ]
        rows.append({"product": str(p.get("partner") or "—"), "by_month": by_month})
    return {"periods": periods, "products": rows}


def narrate_trend(trend_payload):
    """Return {source, narrative, highlights, disclaimer, error?}. Never raises."""
    if not ai_client.is_enabled():
        return {"source": "ai", "narrative": "", "highlights": [], "disclaimer": _DISCLAIMER,
                "error": "AI is not configured. Set the Anthropic key in Configuration → AI."}
    agg = _trend_aggregate(trend_payload)
    if not agg["products"]:
        return {"source": "ai", "narrative": "", "highlights": [], "disclaimer": _DISCLAIMER,
                "error": "No trend data to summarise for this range."}
    user = "Per-product monthly match-rate / open-count table:\n" + json.dumps(agg, separators=(",", ":")) + \
           "\n\nCall trend_summary."
    try:
        data = ai_client.call_tool(_TREND_SYSTEM, user, _TREND_TOOL)
    except Exception as e:
        logger.warning("narrate_trend model call failed: %s", e)
        return {"source": "ai", "narrative": "", "highlights": [], "disclaimer": _DISCLAIMER,
                "error": "AI insight service is unavailable right now."}
    highlights = []
    for h in (data.get("highlights") or []):
        if isinstance(h, dict) and h.get("product"):
            trend = str(h.get("trend") or "flat").lower()
            if trend not in ("improving", "worsening", "flat"):
                trend = "flat"
            highlights.append({"product": str(h["product"])[:60], "trend": trend,
                               "detail": str(h.get("detail") or "").strip()[:300]})
    return {"source": "ai", "narrative": str(data.get("narrative") or "").strip(),
            "highlights": highlights, "disclaimer": _DISCLAIMER}


# ── 2) Escalation root-cause ──────────────────────────────────────────────────

_ESC_SYSTEM = (
    "You are a reconciliation analyst. You are given AGGREGATE statistics about bank-side open "
    "items that have aged beyond 7 days (counts, amount totals, ageing buckets, how many carry a "
    "UTR, per-partner split — no customer data). Propose the most likely ROOT CAUSES and a short, "
    "professional note the team could send to the partner bank. Ground every hypothesis in the "
    "numbers. Typical causes: settlement lag beyond the agreed D+N, the counterpart internal file "
    "not yet uploaded, UTR present on the bank side with no internal match, or partial/failed "
    "settlements. Do not invent specific customers or transactions."
)

_ESC_TOOL = {
    "name": "escalation_brief",
    "description": "Root-cause hypotheses + a partner escalation note for aged open items.",
    "input_schema": {
        "type": "object",
        "properties": {
            "hypotheses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "likelihood": {"type": "string", "enum": ["high", "medium", "low"]},
                        "rationale": {"type": "string", "description": "one line, cite the aggregate numbers"},
                    },
                    "required": ["title", "likelihood"],
                },
            },
            "note": {"type": "string", "description": "a short professional escalation paragraph (no customer PII)"},
        },
        "required": ["hypotheses"],
    },
}


def _escalation_aggregate(items, partner):
    """Build a numbers-only summary of the aged open items (no per-row PII)."""
    ages = [i.get("age_days") or 0 for i in items]
    buckets = {"7-14": 0, "15-30": 0, "31-60": 0, "60+": 0}
    for a in ages:
        if a <= 14: buckets["7-14"] += 1
        elif a <= 30: buckets["15-30"] += 1
        elif a <= 60: buckets["31-60"] += 1
        else: buckets["60+"] += 1
    with_utr = sum(1 for i in items if (i.get("utr") or "").strip())
    per_partner = {}
    for i in items:
        per_partner[i.get("partner") or "—"] = per_partner.get(i.get("partner") or "—", 0) + 1
    return {
        "partner": partner or "all",
        "count": len(items),
        "total_amount": round(sum(i.get("amount") or 0 for i in items), 2),
        "oldest_age_days": max(ages) if ages else 0,
        "ageing_buckets": buckets,
        "with_utr": with_utr,
        "without_utr": len(items) - with_utr,
        "per_partner": per_partner,
    }


def explain_escalation(items, partner=None):
    """Return {source, hypotheses, note, stats, disclaimer, error?}. Never raises."""
    stats = _escalation_aggregate(list(items or []), partner)
    if not ai_client.is_enabled():
        return {"source": "ai", "hypotheses": [], "note": "", "stats": stats, "disclaimer": _DISCLAIMER,
                "error": "AI is not configured. Set the Anthropic key in Configuration → AI."}
    if stats["count"] == 0:
        return {"source": "ai", "hypotheses": [], "note": "", "stats": stats, "disclaimer": _DISCLAIMER,
                "error": "No aged open items to analyse."}
    user = "Aged open-item aggregates:\n" + json.dumps(stats, separators=(",", ":")) + "\n\nCall escalation_brief."
    try:
        data = ai_client.call_tool(_ESC_SYSTEM, user, _ESC_TOOL)
    except Exception as e:
        logger.warning("explain_escalation model call failed: %s", e)
        return {"source": "ai", "hypotheses": [], "note": "", "stats": stats, "disclaimer": _DISCLAIMER,
                "error": "AI insight service is unavailable right now."}
    hyps = []
    for h in (data.get("hypotheses") or []):
        if isinstance(h, dict) and h.get("title"):
            lk = str(h.get("likelihood") or "low").lower()
            if lk not in ("high", "medium", "low"):
                lk = "low"
            hyps.append({"title": str(h["title"])[:200], "likelihood": lk,
                         "rationale": str(h.get("rationale") or "").strip()[:300]})
    return {"source": "ai", "hypotheses": hyps, "note": str(data.get("note") or "").strip(),
            "stats": stats, "disclaimer": _DISCLAIMER}
