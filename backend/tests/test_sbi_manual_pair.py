"""
Tests for the SBI Kiosk two-sided manual PAIR (pair-picker).

Unlike the one-sided SBIManualMatch overlay, this links a specific bank row to a specific
internal row. Like every SBI overlay it is keyed by each side's STABLE BUSINESS key, so the
pair must SURVIVE both a recon re-run AND a file re-upload (which give the source rows new
ids). It must show as Manual_Matched in the unified read model, be reversible, warn (not
block) on amount mismatch, refuse double-linking, and never change rows outside the pair.
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import (
    Base, User, SBIBankTransaction, SBITxnReport, SBIKOLimits,
    SBIP02Result, SBIP03Result, SBIManualPair,
)
from routes.sbi_kiosk import (
    manual_pair_open_items, create_manual_pairs, delete_manual_pair, list_manual_pairs,
    get_unified, ManualPairIn, ManualPairBulkIn,
)

DATE = "2026-06-20"
REMARK = "operator confirmed pair"
USER = User(id="u1", username="raj", role="user", permissions='{"src_assign": true}')


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def _bank(db, ref, ko="KO1", debit=0.0, credit=0.0, settlement=False, deduct=None, date=DATE):
    b = SBIBankTransaction(upload_date=date, txn_date=date, description=f"row {ref}",
                           ref_number=ref, ko_id=ko, debit=debit, credit=credit,
                           is_settlement=settlement, deduct_date=deduct)
    db.add(b); db.commit(); return b


def _report(db, ref, ko="KO1", amount=100.0, date=DATE,
            source_file="AEPS_Withdrawal_Transaction_Report.xlsx",
            txn_type="AEPS OFFUS Withdrawal"):
    t = SBITxnReport(upload_date=date, txn_date=date, source_file=source_file, ko_id=ko,
                     reference_number=ref, amount=amount, txn_type=txn_type, status="Success")
    db.add(t); db.commit(); return t


def _ko(db, ko="KO1", amount=100.0, txn_type="KO Withdrawal", date=DATE, dt=None):
    w = SBIKOLimits(upload_date=date, txn_date=date, ko_id=ko, amount=amount, txn_type=txn_type, txn_datetime=dt)
    db.add(w); db.commit(); return w


def _unified_rows(db):
    return get_unified(recon_date=DATE, page=1, page_size=500, db=db, current_user=USER)["rows"]


def _pair(db, b, t, data_source="Txn Report", remark=REMARK):
    return create_manual_pairs(
        ManualPairBulkIn(pairs=[ManualPairIn(bank_id=b.id, data_id=t.id, data_source=data_source)],
                         remark=remark),
        db=db, current_user=USER)


# ── open-items (pair-picker source) ────────────────────────────────────────────
def test_open_items_lists_unmatched_both_sides_with_file_label(db):
    _bank(db, "R1", debit=100.0)
    _report(db, "R2", amount=100.0)
    _ko(db, txn_type="KO Withdrawal")
    _ko(db, txn_type="KO Deposit")

    bank = manual_pair_open_items(side="bank", date_from=DATE, db=db, current_user=USER)
    data = manual_pair_open_items(side="data", date_from=DATE, db=db, current_user=USER)
    assert bank["total"] == 1
    assert bank["items"][0]["file"] == "Bank Statement"
    # data side spans txn report + KO withdrawal + KO deposit (all three included)
    files = sorted(e["file"] for e in data["items"])
    assert files == ["AEPS Withdrawal Transaction Report", "KO Deposit", "KO Withdrawal"]


def test_open_items_excludes_already_matched(db):
    b = _bank(db, "R1", debit=100.0)
    t = _report(db, "R2", amount=100.0)
    db.add(SBIP02Result(recon_date=DATE, bank_txn_id=b.id, txn_report_id=t.id,
                        reference_number="R2", bank_amount=100.0, bank_type="DR",
                        report_amount=100.0, match_status="Matched")); db.commit()
    assert manual_pair_open_items(side="bank", date_from=DATE, db=db, current_user=USER)["total"] == 0
    assert manual_pair_open_items(side="data", date_from=DATE, db=db, current_user=USER)["total"] == 0


# ── pairing ────────────────────────────────────────────────────────────────────
def test_pair_flips_both_rows_in_unified(db):
    b = _bank(db, "R1", debit=100.0)
    t = _report(db, "R2", amount=100.0)
    out = _pair(db, b, t)
    assert out["paired"] == 1 and out["errors"] == 0 and out["warned"] == 0
    rows = {(r["side"], r["source"]): r for r in _unified_rows(db)}
    bank_row = rows[("bank", "Bank Statement")]
    data_row = rows[("data", "Txn Report")]
    assert bank_row["status"] == "Manual_Matched"
    assert data_row["status"] == "Manual_Matched"
    assert "Txn Report" in bank_row["counterpart"]
    assert "Bank Statement" in data_row["counterpart"]


def test_pair_survives_rerun_and_reupload(db):
    # The whole point: delete & recreate BOTH source rows with new ids but identical
    # business content (a file re-upload / recon re-run) — the pair must still apply.
    b = _bank(db, "R1", debit=100.0)
    t = _report(db, "R2", amount=100.0)
    _pair(db, b, t)
    db.delete(b); db.delete(t); db.commit()
    _bank(db, "R1", debit=100.0)          # new id, same content
    _report(db, "R2", amount=100.0)       # new id, same content
    rows = {(r["side"], r["source"]): r for r in _unified_rows(db)}
    assert rows[("bank", "Bank Statement")]["status"] == "Manual_Matched"
    assert rows[("data", "Txn Report")]["status"] == "Manual_Matched"


def test_unpair_reverts_both_rows(db):
    b = _bank(db, "R1", debit=100.0)
    t = _report(db, "R2", amount=100.0)
    out = _pair(db, b, t)
    pair_id = out["results"][0]["pair_id"]
    delete_manual_pair(pair_id, db=db, current_user=USER)
    rows = {(r["side"], r["source"]): r for r in _unified_rows(db)}
    assert rows[("bank", "Bank Statement")]["status"] == "Not reconciled"
    assert rows[("data", "Txn Report")]["status"] == "Unmatched"
    assert list_manual_pairs(db=db, current_user=USER)["count"] == 0


def test_amount_mismatch_warns_but_pairs(db):
    b = _bank(db, "R1", debit=100.0)
    t = _report(db, "R2", amount=105.0)
    out = _pair(db, b, t)
    assert out["paired"] == 1 and out["warned"] == 1
    assert out["results"][0]["warn"] is True
    assert out["results"][0]["amount_diff"] == 5.0


def test_double_link_rejected(db):
    b = _bank(db, "R1", debit=100.0)
    t1 = _report(db, "R2", amount=100.0)
    t2 = _report(db, "R3", amount=100.0)
    _pair(db, b, t1)
    out = _pair(db, b, t2)                 # same bank row, different report
    assert out["paired"] == 0 and out["errors"] == 1
    assert "already" in out["results"][0]["error"].lower()


def test_ko_deposit_available_in_picker_but_not_in_unified_page(db):
    _ko(db, txn_type="KO Deposit")
    # visible to the pair-picker
    picker = manual_pair_open_items(side="data", date_from=DATE, db=db, current_user=USER)
    assert any(e["file"] == "KO Deposit" for e in picker["items"])
    # but NOT added to the unified page / all-entries report (include_deposits stays False)
    assert all(r["source"] != "KO Deposit" for r in _unified_rows(db))


def test_pair_leaves_unrelated_rows_untouched(db):
    b1 = _bank(db, "R1", debit=100.0)
    _bank(db, "R9", debit=200.0)           # unrelated bank row
    t = _report(db, "R2", amount=100.0)
    _pair(db, b1, t)
    rows = {r["ref"]: r for r in _unified_rows(db) if r["side"] == "bank"}
    assert rows["R1"]["status"] == "Manual_Matched"
    assert rows["R9"]["status"] == "Not reconciled"   # isolation: untouched


def test_remark_required(db):
    b = _bank(db, "R1", debit=100.0)
    t = _report(db, "R2", amount=100.0)
    with pytest.raises(HTTPException) as e:
        _pair(db, b, t, remark="x")
    assert e.value.status_code == 400


# ── collision safety (the adversarial-review defect) ────────────────────────────
def test_duplicate_identical_rows_no_phantom_reconciliation(db):
    # Two business-identical KO withdrawals (same ko/amount/date, no datetime -> same disc).
    # Pairing ONE bank debit to ONE of them must flip EXACTLY ONE — never phantom-reconcile
    # both (which would understate unmatched money in the finance-ops export).
    b = _bank(db, "R1", debit=500.0, ko="KO1")
    w1 = _ko(db, ko="KO1", amount=500.0)   # identical
    _ko(db, ko="KO1", amount=500.0)        # identical duplicate
    out = _pair(db, b, w1, data_source="KO Withdrawal")
    assert out["paired"] == 1
    ko_rows = [r for r in _unified_rows(db) if r["source"] == "KO Withdrawal"]
    matched = [r for r in ko_rows if r["status"] == "Manual_Matched"]
    assert len(ko_rows) == 2 and len(matched) == 1   # exactly one flips, the other stays open


def test_distinct_datetime_rows_can_both_be_paired(db):
    # Same ko/amount/date but DIFFERENT txn_datetime -> distinct keys -> each pairs cleanly.
    b1 = _bank(db, "R1", debit=500.0, ko="KO1")
    b2 = _bank(db, "R2", debit=500.0, ko="KO1")
    w1 = _ko(db, ko="KO1", amount=500.0, dt="2026-06-20 09:00:00")
    w2 = _ko(db, ko="KO1", amount=500.0, dt="2026-06-20 15:30:00")
    assert _pair(db, b1, w1, data_source="KO Withdrawal")["paired"] == 1
    assert _pair(db, b2, w2, data_source="KO Withdrawal")["paired"] == 1
    ko_rows = [r for r in _unified_rows(db) if r["source"] == "KO Withdrawal"]
    assert all(r["status"] == "Manual_Matched" for r in ko_rows) and len(ko_rows) == 2


# ── internal ↔ internal: KO Withdrawal ↔ KO Deposit (Rajendra 2026-08) ──────────
def _pair_data(db, left, left_source, right, right_source, remark=REMARK):
    """Pair two DATA-side rows (left leg flagged via bank_data_source)."""
    return create_manual_pairs(
        ManualPairBulkIn(pairs=[ManualPairIn(bank_id=left.id, bank_data_source=left_source,
                                             data_id=right.id, data_source=right_source)],
                         remark=remark),
        db=db, current_user=USER)


def test_ko_withdrawal_paired_with_ko_deposit(db):
    w = _ko(db, ko="KO1", amount=56000.0, txn_type="KO Withdrawal")
    d = _ko(db, ko="KO1", amount=56000.0, txn_type="KO Deposit")
    out = _pair_data(db, w, "KO Withdrawal", d, "KO Deposit")
    assert out["paired"] == 1 and out["errors"] == 0
    # KO Withdrawal flips to Manual_Matched in the unified page; counterpart is the KO Deposit
    ko_rows = [r for r in _unified_rows(db) if r["source"] == "KO Withdrawal"]
    assert len(ko_rows) == 1 and ko_rows[0]["status"] == "Manual_Matched"
    assert "KO Deposit" in ko_rows[0]["counterpart"]
    # both rows leave the pair-picker (both now closed)
    data = manual_pair_open_items(side="data", date_from=DATE, db=db, current_user=USER)
    kinds = [e["file"] for e in data["items"]]
    assert "KO Withdrawal" not in kinds and "KO Deposit" not in kinds


def test_ko_pair_unpair_reopens_both(db):
    w = _ko(db, ko="KO1", amount=56000.0, txn_type="KO Withdrawal")
    d = _ko(db, ko="KO1", amount=56000.0, txn_type="KO Deposit")
    out = _pair_data(db, w, "KO Withdrawal", d, "KO Deposit")
    delete_manual_pair(out["results"][0]["pair_id"], db=db, current_user=USER)
    data = manual_pair_open_items(side="data", date_from=DATE, db=db, current_user=USER)
    kinds = sorted(e["file"] for e in data["items"])
    assert "KO Withdrawal" in kinds and "KO Deposit" in kinds   # both open again


def test_ko_deposit_cannot_be_paired_twice(db):
    # a KO Deposit already used as the right leg can't be reused (merged used_keys across legs)
    w1 = _ko(db, ko="KO1", amount=56000.0, txn_type="KO Withdrawal", dt="2026-06-20 09:00:00")
    w2 = _ko(db, ko="KO1", amount=56000.0, txn_type="KO Withdrawal", dt="2026-06-20 10:00:00")
    d = _ko(db, ko="KO1", amount=56000.0, txn_type="KO Deposit")
    assert _pair_data(db, w1, "KO Withdrawal", d, "KO Deposit")["paired"] == 1
    out = _pair_data(db, w2, "KO Withdrawal", d, "KO Deposit")   # reuse the same deposit
    assert out["paired"] == 0 and out["errors"] == 1
    assert "already" in out["results"][0]["error"].lower()


def test_report_open_bank_credit_shows_even_when_unified_ghost_matched(db):
    """Rajendra 2026-08: a bank CREDIT the unified view marks 'Matched' via the weak (csp,amount)
    P03 overlay, but which the reconciliation report leaves Unmatched (its 20-digit ref isn't in
    any source file), must STILL appear in the pair-picker's bank side — the report is the source
    of truth there, so a report-open row is a real open item even if the unified status says
    Matched. (It was being hidden by the picker's status-closed filter.)"""
    REF = "62101614158300006350"                  # a 20-digit bank ref, not in any source file
    _bank(db, REF, ko="CSP1", credit=15000.0)      # money-IN credit
    db.add(SBIP03Result(recon_date=DATE, csp_code="CSP1", txn_amount=15000.0,
                        bank_amount=15000.0, match_status="Matched")); db.commit()
    # unified view ghost-matches it via (csp, amount)
    bank_row = next(r for r in _unified_rows(db) if r["ref"] == REF and r["side"] == "bank")
    assert bank_row["status"] == "Matched"
    # ...but the report leaves it Unmatched, so the picker MUST list it as an open bank item
    pick = manual_pair_open_items(side="bank", date_from=DATE, db=db, current_user=USER)
    assert any(e["ref"] == REF for e in pick["items"])


def test_resolved_keys_path_is_id_independent(db):
    # Simulates the maker-checker replay: the pair is re-created from STABLE keys with no
    # source-row ids (which regenerate on re-upload, #17), and still applies.
    b = _bank(db, "R1", debit=100.0)
    t = _report(db, "R2", amount=100.0)
    _pair(db, b, t)
    mp = db.query(SBIManualPair).first()
    keys = dict(bank_key=mp.bank_key, data_key=mp.data_key, bank_source=mp.bank_source,
                data_source=mp.data_source, bank_amount=mp.bank_amount, data_amount=mp.data_amount,
                bank_date=mp.bank_date, data_date=mp.data_date)
    db.delete(mp); db.commit()            # drop it so we can re-create via the id-free path
    out = create_manual_pairs(ManualPairBulkIn(pairs=[ManualPairIn(**keys)], remark="replay path ok"),
                              db=db, current_user=USER)
    assert out["paired"] == 1
    rows = {(r["side"], r["source"]): r for r in _unified_rows(db)}
    assert rows[("bank", "Bank Statement")]["status"] == "Manual_Matched"
    assert rows[("data", "Txn Report")]["status"] == "Manual_Matched"
