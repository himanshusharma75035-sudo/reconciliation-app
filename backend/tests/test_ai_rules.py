"""
Tests for core.ai_rules — AI-proposed matching rules (advisory).

Network is faked via a stub ai_client, so these run fully offline and assert the
safety-critical behaviour: allowed-field constraint, amount-only rejection,
de-dup against existing rules, confidence gating, de-identified payload, and
graceful degradation.
"""
from types import SimpleNamespace

import core.ai_rules as ai_rules


def _row(rid, amount, **kw):
    base = dict(
        id=rid, amount=amount, recon_date="2026-07-20", transaction_date="2026-07-20",
        dr_cr="CR", status="Success", eko_tid=None, tracking_number=None, utr_number="UTR1",
        bank_account="9876543210123", csp_name="Rajesh Kumar", bank_description="NEFT NARRATION",
        csp_code=None, src_note=None, raw_data=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _stub(monkeypatch, *, enabled=True, rules=None, boom=False, capture=None):
    def call_tool(system, user, tool, **kw):
        if capture is not None:
            capture["user"] = user
        if boom:
            raise RuntimeError("API down")
        return {"rules": rules or []}
    monkeypatch.setattr(ai_rules, "ai_client",
                        SimpleNamespace(is_enabled=lambda: enabled, call_tool=call_tool),
                        raising=True)


def test_disabled_returns_error(monkeypatch):
    _stub(monkeypatch, enabled=False)
    out = ai_rules.suggest_rules([_row("b", 100)], [_row("i", 100)], "fino", [])
    assert out["suggestions"] == [] and "not configured" in out["error"].lower()


def test_maps_valid_rule(monkeypatch):
    _stub(monkeypatch, rules=[{"fields": ["utr_number"], "rationale": "shared utr", "confidence": 90}])
    out = ai_rules.suggest_rules([_row("b", 100)], [_row("i", 100)], "fino", [])
    assert len(out["suggestions"]) == 1
    s = out["suggestions"][0]
    assert s["source"] == "ai" and s["fields"] == ["utr_number"] and s["partner"] == "fino"


def test_disallowed_fields_are_stripped(monkeypatch):
    # Model tries to propose a PII/unknown field — it must be filtered out.
    _stub(monkeypatch, rules=[{"fields": ["bank_account", "utr_number"], "confidence": 80}])
    out = ai_rules.suggest_rules([_row("b", 100)], [_row("i", 100)], "fino", [])
    assert out["suggestions"][0]["fields"] == ["utr_number"]


def test_amount_only_rule_rejected(monkeypatch):
    _stub(monkeypatch, rules=[{"fields": ["amount"], "confidence": 95}])
    out = ai_rules.suggest_rules([_row("b", 100)], [_row("i", 100)], "fino", [])
    assert out["suggestions"] == []


def test_existing_rule_deduped(monkeypatch):
    _stub(monkeypatch, rules=[{"fields": ["utr_number"], "confidence": 90}])
    out = ai_rules.suggest_rules([_row("b", 100)], [_row("i", 100)], "fino",
                                 existing_field_sets=[["utr_number"]])
    assert out["suggestions"] == []


def test_low_confidence_dropped(monkeypatch):
    _stub(monkeypatch, rules=[{"fields": ["utr_number", "amount"], "confidence": 10}])
    out = ai_rules.suggest_rules([_row("b", 100)], [_row("i", 100)], "fino", [])
    assert out["suggestions"] == []


def test_payload_has_no_pii(monkeypatch):
    cap = {}
    _stub(monkeypatch, rules=[], capture=cap)
    ai_rules.suggest_rules([_row("BANK-A", 100, bank_account="9876543210123", csp_name="Rajesh Kumar")],
                           [_row("INT-A", 100)], "fino", [])
    blob = cap["user"].lower()
    assert "9876543210123" not in blob and "rajesh" not in blob and "bank-a" not in blob


def test_model_failure_graceful(monkeypatch):
    _stub(monkeypatch, boom=True)
    out = ai_rules.suggest_rules([_row("b", 100)], [_row("i", 100)], "fino", [])
    assert out["suggestions"] == [] and "error" in out
