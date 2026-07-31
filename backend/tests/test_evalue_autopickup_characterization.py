"""
Characterization tests for the E-Value AUTO-PICKUP ingest path (ingest_bank_bytes).

The two E-Value ingest copies were unified into one shared core in commit 4df3068
(behaviour-contract item 10). The interactive HTTP path is pinned by
test_evalue_incremental_upload.py, but the watch-folder AUTO path had ZERO coverage —
yet it is exactly the path that used to full-account-wipe and skip the SHA-256 guard.
These lock the auto path's current behaviour so a future edit to the shared helpers or
the guard ordering can't silently re-introduce data loss the moment the watch folder is
enabled. They pin behaviour AS-IS (a green run proves reality, not a wish).
"""
import io

import pytest
import openpyxl
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException

from models.database import Base, EvalueAccount, EvalueBankTxn
from routes.evalue import ingest_bank_bytes

ACCT = "ICIC-8580"; BANK = "ICICI BANK"; ACCTNO = "000705048580"
_HDR = ["S.N.", "Tran. Id", "Value Date", "Transaction Date", "Transaction Posted Date",
        "Cheque. No./Ref. No.", "Transaction Remarks", "Withdrawal Amt (INR)",
        "Deposit Amt (INR)", "Balance (INR)"]


@pytest.fixture
def db():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    s = sessionmaker(bind=e, autoflush=False, autocommit=False)()
    s.add(EvalueAccount(bank_name=BANK, account_number=ACCTNO, reco_acc_no=ACCT))
    s.commit()
    yield s
    s.close()


def _icici(date_ddmon, remark="IMPS/x", withdrawal="1000.00", balance="5000.00"):
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Sheet0"
    for r in [["Detailed Statement"], ["A/C No:", ACCTNO], [], _HDR,
              ["1", "S51619732", date_ddmon, date_ddmon, f"{date_ddmon} 10:17:56 AM", "",
               remark, withdrawal, "", balance]]:
        ws.append(r)
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def _icici_header_only():
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Sheet0"
    for r in [["Detailed Statement"], ["A/C No:", ACCTNO], [], _HDR]:
        ws.append(r)
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def _dates(db):
    return sorted({r.txn_date for r in db.query(EvalueBankTxn)
                   .filter(EvalueBankTxn.reco_acc_no == ACCT).all()})


def _ingest(db, raw, fn):
    return ingest_bank_bytes(raw, fn, BANK, ACCT, db)


def test_autopickup_bank_accumulates_per_date_not_wipe(db):
    # THE data-loss guard: a second day's file must NOT wipe the first (the old bug).
    _ingest(db, _icici("01/Jul/2026"), "d1.xls")
    assert _dates(db) == ["2026-07-01"]
    _ingest(db, _icici("02/Jul/2026", remark="IMPS/day2", withdrawal="2000.00"), "d2.xls")
    assert _dates(db) == ["2026-07-01", "2026-07-02"]


def test_autopickup_bank_same_date_supersedes_only_that_date(db):
    _ingest(db, _icici("01/Jul/2026", withdrawal="1000.00"), "d1.xls")
    _ingest(db, _icici("02/Jul/2026", withdrawal="2000.00"), "d2.xls")
    _ingest(db, _icici("01/Jul/2026", remark="IMPS/CORRECTED", withdrawal="1500.00"), "d1b.xls")
    assert _dates(db) == ["2026-07-01", "2026-07-02"]        # Jul 2 preserved
    jul1 = [r for r in db.query(EvalueBankTxn).filter(EvalueBankTxn.reco_acc_no == ACCT).all()
            if r.txn_date == "2026-07-01"]
    assert len(jul1) == 1 and jul1[0].amount == 1500.0       # superseded, not duplicated


def test_autopickup_duplicate_file_raises_409(db):
    raw = _icici("01/Jul/2026")
    _ingest(db, raw, "d1.xls")
    with pytest.raises(HTTPException) as ex:
        _ingest(db, raw, "d1.xls")                           # identical bytes re-scanned → SHA guard
    assert ex.value.status_code == 409


def test_autopickup_zero_row_file_records_no_hash(db):
    # A file that parses to ZERO rows must record NO hash (guard is AFTER the zero-row check),
    # so a later REAL file — even with the same name — still ingests instead of being blocked.
    r0 = _ingest(db, _icici_header_only(), "stmt.xls")
    assert r0["rows"] == 0
    _ingest(db, _icici("01/Jul/2026"), "stmt.xls")
    assert _dates(db) == ["2026-07-01"]
