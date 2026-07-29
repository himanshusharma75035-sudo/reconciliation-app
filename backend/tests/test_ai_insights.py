"""
Tests for core.ai_insights — AI narratives from PII-free aggregates (advisory).

Offline (faked ai_client). Assert: aggregate correctness (ageing buckets, UTR
counts), graceful degradation, tagged source, and that the payload sent to the
model contains only numbers/labels (the escalation aggregate never carries a
customer id / narration).
"""
from types import SimpleNamespace

import core.ai_insights as ai_insights


def _stub(monkeypatch, *, enabled=True, ret=None, boom=False, capture=None):
    def call_tool(system, user, tool, **kw):
        if capture is not None:
            capture["user"] = user
        if boom:
            raise RuntimeError("API down")
        return ret or {}
    monkeypatch.setattr(ai_insights, "ai_client",
                        SimpleNamespace(is_enabled=lambda: enabled, call_tool=call_tool),
                        raising=True)


# ── trend narrative ───────────────────────────────────────────────────────────

_TREND = {
    "periods": ["2026-05", "2026-06", "2026-07"],
    "partners": [
        {"partner": "Fino", "by_month": [
            {"match_rate": 98.0, "matched": 98, "total": 100, "open": 2},
            {"match_rate": 95.0, "matched": 95, "total": 100, "open": 5},
            {"match_rate": 88.0, "matched": 88, "total": 100, "open": 12}]},
    ],
}


def test_trend_disabled_error(monkeypatch):
    _stub(monkeypatch, enabled=False)
    out = ai_insights.narrate_trend(_TREND)
    assert out["narrative"] == "" and "not configured" in out["error"].lower()
    assert out["source"] == "ai"


def test_trend_maps_highlights(monkeypatch):
    _stub(monkeypatch, ret={"narrative": "Fino is sliding.",
                            "highlights": [{"product": "Fino", "trend": "worsening", "detail": "98→88%"}]})
    out = ai_insights.narrate_trend(_TREND)
    assert out["narrative"] == "Fino is sliding."
    assert out["highlights"][0]["trend"] == "worsening"
    assert out["disclaimer"]


def test_trend_empty_data_error(monkeypatch):
    _stub(monkeypatch)
    out = ai_insights.narrate_trend({"periods": [], "partners": []})
    assert "error" in out


def test_trend_bad_trend_value_coerced(monkeypatch):
    _stub(monkeypatch, ret={"narrative": "x", "highlights": [{"product": "Fino", "trend": "nonsense", "detail": "d"}]})
    out = ai_insights.narrate_trend(_TREND)
    assert out["highlights"][0]["trend"] == "flat"


# ── escalation root-cause ─────────────────────────────────────────────────────

def _item(age, amount, utr=None, partner="fino"):
    return {"id": "x", "partner": partner, "recon_date": "2026-07-01", "age_days": age,
            "eko_tid": "TID1", "utr": utr, "amount": amount}


def test_escalation_aggregate_buckets_and_utr():
    items = [_item(10, 100, "U1"), _item(20, 200), _item(45, 300, "U3"), _item(90, 400)]
    stats = ai_insights._escalation_aggregate(items, "fino")
    assert stats["count"] == 4 and stats["total_amount"] == 1000
    assert stats["ageing_buckets"] == {"7-14": 1, "15-30": 1, "31-60": 1, "60+": 1}
    assert stats["with_utr"] == 2 and stats["without_utr"] == 2
    assert stats["oldest_age_days"] == 90


def test_escalation_disabled_error(monkeypatch):
    _stub(monkeypatch, enabled=False)
    out = ai_insights.explain_escalation([_item(10, 100)], "fino")
    assert out["hypotheses"] == [] and "not configured" in out["error"].lower()
    assert out["stats"]["count"] == 1   # stats computed even when AI disabled


def test_escalation_maps_hypotheses(monkeypatch):
    _stub(monkeypatch, ret={"hypotheses": [{"title": "Settlement lag", "likelihood": "high", "rationale": "60+ bucket"}],
                            "note": "Please confirm settlement."})
    out = ai_insights.explain_escalation([_item(90, 400)], "fino")
    assert out["hypotheses"][0]["likelihood"] == "high"
    assert out["note"] == "Please confirm settlement."


def test_escalation_no_items_error(monkeypatch):
    _stub(monkeypatch)
    out = ai_insights.explain_escalation([], "fino")
    assert "error" in out and out["stats"]["count"] == 0


def test_escalation_payload_is_aggregate_only(monkeypatch):
    cap = {}
    _stub(monkeypatch, ret={"hypotheses": []}, capture=cap)
    # eko_tid present on the item must NOT reach the model (aggregate only).
    ai_insights.explain_escalation([_item(10, 100, "U1")], "fino")
    blob = cap["user"].lower()
    assert "tid1" not in blob and "u1" not in blob   # no per-row references leak


def test_escalation_model_failure_graceful(monkeypatch):
    _stub(monkeypatch, boom=True)
    out = ai_insights.explain_escalation([_item(10, 100)], "fino")
    assert out["hypotheses"] == [] and "error" in out
