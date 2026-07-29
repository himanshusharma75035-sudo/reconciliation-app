"""
Tests for core.ai_anomalies — AI anomaly detection (advisory).

Offline (faked ai_client). Assert: token→row_id resolution, hallucinated-token
rejection, severity ordering, de-identified payload, graceful degradation.
"""
from types import SimpleNamespace

import core.ai_anomalies as ai_anomalies


def _row(rid, amount, **kw):
    base = dict(
        id=rid, amount=amount, partner="fino", recon_date="2026-07-20",
        transaction_date="2026-07-20", dr_cr="CR", status="Success",
        eko_tid=None, tracking_number=None, utr_number="UTRREF0",
        bank_account="9876543210123", csp_name="Rajesh Kumar", bank_description="NEFT",
        csp_code=None, src_note=None, raw_data=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _stub(monkeypatch, *, enabled=True, anomalies=None, boom=False, capture=None):
    def call_tool(system, user, tool, **kw):
        if capture is not None:
            capture["user"] = user
        if boom:
            raise RuntimeError("API down")
        return {"anomalies": anomalies or []}
    monkeypatch.setattr(ai_anomalies, "ai_client",
                        SimpleNamespace(is_enabled=lambda: enabled, call_tool=call_tool),
                        raising=True)


def test_disabled_returns_error(monkeypatch):
    _stub(monkeypatch, enabled=False)
    out = ai_anomalies.detect_anomalies([_row("a", 100)])
    assert out["ai_anomalies"] == [] and "not configured" in out["error"].lower()
    assert out["source"] == "ai"


def test_empty_rows_no_error(monkeypatch):
    _stub(monkeypatch)
    out = ai_anomalies.detect_anomalies([])
    assert out["ai_anomalies"] == [] and "error" not in out


def test_maps_tokens_to_row_ids(monkeypatch):
    rows = [_row("ROW-1", 500), _row("ROW-2", 500)]
    _stub(monkeypatch, anomalies=[
        {"kind": "same_amount_diff_ref", "severity": "high", "title": "Two ₹500 different refs",
         "detail": "T1 and T2", "refs": ["T1", "T2"]},
    ])
    out = ai_anomalies.detect_anomalies(rows)
    assert len(out["ai_anomalies"]) == 1
    a = out["ai_anomalies"][0]
    assert a["source"] == "ai" and a["severity"] == "high"
    assert a["row_ids"] == ["ROW-1", "ROW-2"]
    assert a["amount"] == 500 and a["partner"] == "fino"


def test_hallucinated_tokens_dropped(monkeypatch):
    rows = [_row("ROW-1", 500)]
    _stub(monkeypatch, anomalies=[
        {"kind": "x", "severity": "low", "title": "real", "refs": ["T1"]},
        {"kind": "y", "severity": "low", "title": "invented", "refs": ["T999"]},
    ])
    out = ai_anomalies.detect_anomalies(rows)
    assert len(out["ai_anomalies"]) == 1
    assert out["ai_anomalies"][0]["title"] == "real"


def test_severity_ordering(monkeypatch):
    rows = [_row("R1", 1), _row("R2", 2), _row("R3", 3)]
    _stub(monkeypatch, anomalies=[
        {"kind": "a", "severity": "low", "title": "low", "refs": ["T1"]},
        {"kind": "b", "severity": "high", "title": "high", "refs": ["T2"]},
        {"kind": "c", "severity": "medium", "title": "med", "refs": ["T3"]},
    ])
    out = ai_anomalies.detect_anomalies(rows)
    assert [a["severity"] for a in out["ai_anomalies"]] == ["high", "medium", "low"]


def test_payload_has_no_pii(monkeypatch):
    cap = {}
    _stub(monkeypatch, anomalies=[], capture=cap)
    ai_anomalies.detect_anomalies([_row("ROW-1", 500, bank_account="9876543210123", csp_name="Rajesh Kumar")])
    blob = cap["user"].lower()
    assert "9876543210123" not in blob and "rajesh" not in blob and "row-1" not in blob


def test_model_failure_graceful(monkeypatch):
    _stub(monkeypatch, boom=True)
    out = ai_anomalies.detect_anomalies([_row("a", 100)])
    assert out["ai_anomalies"] == [] and "error" in out
