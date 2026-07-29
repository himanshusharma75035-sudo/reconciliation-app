"""
Tests for core.ai_matcher — AI-assisted suggestions (advisory, human-approved).

The network call (_call_model) is monkeypatched, so these run fully offline and
assert the safety-critical behaviour: token→id mapping, hallucinated-token
rejection, 1-to-1 pairing, confidence gating, graceful degradation when disabled,
and that suggestions come back in the same shape as the heuristic endpoint.
"""
from types import SimpleNamespace

import core.ai_matcher as ai_matcher


def _row(rid, amount, **kw):
    base = dict(
        id=rid, amount=amount, recon_date="2026-07-20",
        transaction_date="2026-07-20", dr_cr="CR", status="Success",
        eko_tid=None, tracking_number=None, utr_number=None,
        bank_account="9876543210123", csp_name="Rajesh Kumar",
        bank_description="NEFT NARRATION", csp_code=None, src_note=None, raw_data=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _enable(monkeypatch):
    monkeypatch.setattr(ai_matcher, "portal_agent",
                        SimpleNamespace(is_enabled=lambda: True, _api_key=lambda: "sk-test"),
                        raising=False)


def test_disabled_returns_error_not_exception(monkeypatch):
    monkeypatch.setattr(ai_matcher, "portal_agent",
                        SimpleNamespace(is_enabled=lambda: False), raising=False)
    out = ai_matcher.suggest_matches([_row("b1", 100)], [_row("i1", 100)])
    assert out["suggestions"] == []
    assert "not configured" in out["error"].lower()


def test_empty_sides_no_error(monkeypatch):
    _enable(monkeypatch)
    out = ai_matcher.suggest_matches([], [_row("i1", 100)])
    assert out["suggestions"] == [] and "error" not in out


def test_maps_tokens_back_to_real_ids(monkeypatch):
    _enable(monkeypatch)
    bank = [_row("BANK-A", 500, utr_number="UTR1"), _row("BANK-B", 900, utr_number="UTR2")]
    internal = [_row("INT-A", 500, utr_number="UTR1"), _row("INT-B", 900, utr_number="UTR2")]
    monkeypatch.setattr(ai_matcher, "_call_model", lambda b, i: [
        {"bank": "B1", "internal": "I1", "confidence": 95, "reason": "same UTR"},
        {"bank": "B2", "internal": "I2", "confidence": 80, "reason": "same UTR"},
    ])
    out = ai_matcher.suggest_matches(bank, internal)
    s = out["suggestions"]
    assert len(s) == 2
    assert s[0]["source"] == "ai" and s[0]["score"] == 95
    assert s[0]["bank"]["id"] == "BANK-A" and s[0]["internal"]["id"] == "INT-A"
    assert s[1]["bank"]["id"] == "BANK-B" and s[1]["internal"]["id"] == "INT-B"


def test_hallucinated_token_is_dropped(monkeypatch):
    _enable(monkeypatch)
    bank, internal = [_row("BANK-A", 500)], [_row("INT-A", 500)]
    monkeypatch.setattr(ai_matcher, "_call_model", lambda b, i: [
        {"bank": "B1", "internal": "I1", "confidence": 90, "reason": "ok"},
        {"bank": "B999", "internal": "I1", "confidence": 90, "reason": "invented token"},
        {"bank": "B1", "internal": "I42", "confidence": 90, "reason": "invented token"},
    ])
    out = ai_matcher.suggest_matches(bank, internal)
    assert len(out["suggestions"]) == 1
    assert out["suggestions"][0]["bank"]["id"] == "BANK-A"


def test_low_confidence_is_filtered(monkeypatch):
    _enable(monkeypatch)
    bank, internal = [_row("BANK-A", 500)], [_row("INT-A", 500)]
    monkeypatch.setattr(ai_matcher, "_call_model", lambda b, i: [
        {"bank": "B1", "internal": "I1", "confidence": 10, "reason": "weak"},
    ])
    out = ai_matcher.suggest_matches(bank, internal)
    assert out["suggestions"] == []


def test_no_one_to_many_reuse(monkeypatch):
    _enable(monkeypatch)
    bank = [_row("BANK-A", 500), _row("BANK-B", 500)]
    internal = [_row("INT-A", 500)]
    # Model tries to pair the same internal row with two bank rows.
    monkeypatch.setattr(ai_matcher, "_call_model", lambda b, i: [
        {"bank": "B1", "internal": "I1", "confidence": 90, "reason": "first"},
        {"bank": "B2", "internal": "I1", "confidence": 90, "reason": "reuse"},
    ])
    out = ai_matcher.suggest_matches(bank, internal)
    assert len(out["suggestions"]) == 1  # internal row used at most once


def test_model_receives_no_pii(monkeypatch):
    """The payloads handed to _call_model must be de-identified."""
    _enable(monkeypatch)
    captured = {}
    def _capture(b_payload, i_payload):
        captured["b"] = b_payload
        captured["i"] = i_payload
        return []
    monkeypatch.setattr(ai_matcher, "_call_model", _capture)
    ai_matcher.suggest_matches(
        [_row("BANK-A", 500, bank_account="9876543210123", csp_name="Rajesh Kumar")],
        [_row("INT-A", 500)],
    )
    import json
    blob = (json.dumps(captured.get("b", [])) + json.dumps(captured.get("i", []))).lower()
    assert "9876543210123" not in blob
    assert "rajesh" not in blob
    assert "bank-a" not in blob  # real id never sent, only tokens


def test_model_failure_degrades_gracefully(monkeypatch):
    _enable(monkeypatch)
    def _boom(b, i):
        raise RuntimeError("API down")
    monkeypatch.setattr(ai_matcher, "_call_model", _boom)
    out = ai_matcher.suggest_matches([_row("b1", 100)], [_row("i1", 100)])
    assert out["suggestions"] == [] and "error" in out
