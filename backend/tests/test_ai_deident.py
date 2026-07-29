"""
Tests for core.ai_deident — the de-identification choke-point.

The one thing that must never regress: customer PII (account numbers, customer /
retailer names, bank narration, raw rows) must never appear in the payload that
goes to the model, and the model must only ever see opaque tokens for row ids.
"""
import json
from types import SimpleNamespace

import pytest

from core.ai_deident import deidentify_rows, assert_no_pii, SAFE_FIELDS


def _row(**kw):
    """A stand-in for an ORM Transaction row (ai_deident only reads attributes)."""
    base = dict(
        id="real-uuid-1234", amount=1500.0, recon_date="2026-07-20",
        transaction_date="2026-07-20 11:02:00", dr_cr="CR", status="Success",
        eko_tid="TID99887766", tracking_number="TRK55443322", utr_number="UTR12345678901",
        # sensitive fields that must NEVER be emitted:
        bank_account="9876543210123", csp_name="Rajesh Kumar Retail",
        csp_code="CSP7788", bank_description="NEFT FROM RAJESH KUMAR AC 9876543210123",
        src_note="settled to CSP7788", raw_data='{"AccountNo":"9876543210123"}',
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_payload_keys_are_allowlist_only():
    payload, _ = deidentify_rows([_row()], "B")
    assert len(payload) == 1
    for rec in payload:
        assert set(rec.keys()) <= set(SAFE_FIELDS), f"unexpected key: {rec.keys()}"


def test_real_id_never_in_payload_only_token():
    payload, token_map = deidentify_rows([_row(id="real-uuid-1234")], "B")
    blob = json.dumps(payload)
    assert "real-uuid-1234" not in blob         # real id never sent
    assert payload[0]["ref"] == "B1"            # only an opaque token
    assert token_map["B1"] == "real-uuid-1234"  # map kept server-side


def test_pii_values_absent_from_payload():
    row = _row()
    payload, _ = deidentify_rows([row], "I")
    blob = json.dumps(payload).lower()
    for leaked in ("9876543210123", "rajesh", "csp7788", "neft from"):
        assert leaked not in blob, f"PII leaked into payload: {leaked}"


def test_assert_no_pii_passes_on_clean_payload():
    rows = [_row()]
    payload, _ = deidentify_rows(rows, "B")
    assert assert_no_pii(payload, rows) is True


def test_assert_no_pii_raises_when_account_number_leaks():
    rows = [_row(bank_account="9876543210123")]
    # A hand-crafted BAD payload that (wrongly) contains the account number.
    bad = [{"ref": "B1", "utr": "9876543210123"}]
    with pytest.raises(ValueError):
        assert_no_pii(bad, rows)


def test_assert_no_pii_raises_when_name_leaks():
    rows = [_row(csp_name="Rajesh Kumar Retail")]
    bad = [{"ref": "B1", "status": "paid to Rajesh Kumar Retail"}]
    with pytest.raises(ValueError):
        assert_no_pii(bad, rows)


def test_tokens_are_unique_and_sequential():
    rows = [_row(id=f"id-{i}") for i in range(3)]
    payload, token_map = deidentify_rows(rows, "I")
    assert [r["ref"] for r in payload] == ["I1", "I2", "I3"]
    assert token_map == {"I1": "id-0", "I2": "id-1", "I3": "id-2"}


def test_none_fields_are_dropped_not_sent_as_null():
    row = _row(dr_cr=None, status="", eko_tid=None, tracking_number="", utr_number=None)
    payload, _ = deidentify_rows([row], "B")
    rec = payload[0]
    assert "drcr" not in rec and "status" not in rec
    assert "tid" not in rec and "track" not in rec and "utr" not in rec
    # amount/date/ref always present
    assert rec["ref"] == "B1" and "amount" in rec and "date" in rec
