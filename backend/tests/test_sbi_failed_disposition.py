"""
SBI Kiosk 'Failed (closed)' disposition (Rajendra 2026-07): a source-FAILED txn that never
reconciled is CLOSED (no money moved) — it must carry the "Failed" unified status, drop out of
the pair-picker, and stay a DISTINCT bucket (never folded into Matched). Derived read-time from
the stored status (Success-whitelist inverse) so a Success is never reclassified and existing
rows work without re-ingest. A failed txn that DID move money (P02-Matched) must stay Matched.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, SBITxnReport, SBIP02Result
from routes.sbi_kiosk import (_unified_entries, manual_pair_open_items,
                              _MATCHED_UNIFIED, _CLOSED_UNIFIED, _is_txn_failed)

RD = "2026-06-25"


@pytest.fixture
def db():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    s = sessionmaker(bind=e, autoflush=False, autocommit=False)()
    def txr(_id, status, amt):
        s.add(SBITxnReport(id=_id, txn_date=RD, amount=amt, reference_number=f"R{_id}",
                           ko_id="KO1", txn_type="Money Transfer", source_file="AEPS", status=status))
    txr("ok", "Success", 100.0)               # success, unmatched -> stays Unmatched
    txr("f1", "Failure", 200.0)               # -> Failed
    txr("f2", "T_EXP", 300.0)                 # -> Failed
    txr("f3", "Failure/Timed Out", 400.0)     # -> Failed
    txr("bl", "", 500.0)                       # blank status -> stays Unmatched (never Failed)
    txr("fm", "Failure", 600.0)               # failed BUT P02-matched (money moved) -> stays Matched
    s.add(SBIP02Result(id="p1", recon_date=RD, txn_report_id="fm", match_status="Matched",
                       bank_type="CR", bank_amount=600.0, success_status="Failure"))
    s.commit()
    yield s
    s.close()


def _status(db):
    return {e["id"]: e["status"] for e in _unified_entries(db, RD) if e["side"] == "data"}


def test_is_txn_failed_is_success_whitelist_inverse():
    assert _is_txn_failed("Failure") and _is_txn_failed("T_EXP") and _is_txn_failed("Failure/Timed Out")
    assert _is_txn_failed("  failure ")                     # case/space-insensitive
    assert not _is_txn_failed("Success") and not _is_txn_failed("success")
    assert not _is_txn_failed("") and not _is_txn_failed(None)   # blank never failed


def test_failed_rows_get_failed_status(db):
    st = _status(db)
    assert st["f1"] == "Failed" and st["f2"] == "Failed" and st["f3"] == "Failed"


def test_success_and_blank_never_reclassified(db):
    st = _status(db)
    assert st["ok"] == "Unmatched"      # a Success gap is a real exception, not Failed
    assert st["bl"] == "Unmatched"      # blank/unknown status left alone


def test_failed_but_money_moved_stays_matched(db):
    # the ~739-row safety case: failed txn WITH a P02 bank leg must NOT be hidden as Failed
    assert _status(db)["fm"] == "Matched"


def test_failed_is_distinct_closed_bucket_not_matched():
    assert "Failed" in _CLOSED_UNIFIED and "Failed" not in _MATCHED_UNIFIED


def test_picker_excludes_failed_keeps_open(db):
    res = manual_pair_open_items(side="data", date_from=RD, date_to=RD, db=db, current_user=None)
    ids = {i["id"] for i in res["items"]}
    assert "ok" in ids and "bl" in ids                      # real open items remain
    assert ids.isdisjoint({"f1", "f2", "f3", "fm"})         # failed + matched are gone from the picker
