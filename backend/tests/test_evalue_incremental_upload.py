"""
E-Value bank upload must ACCUMULATE across days, not wipe history.

Bug (found by finance ops, 2026-07): upload-bank ran a full-account delete on
every upload, so operators uploading single-day statements lost every prior day
from EvalueBankTxn — while the funds snapshots (per-date upsert) survived, so the
EOD-balance email still showed amounts the recon report had lost. Fix: delete only
the txn_dates the uploaded file covers, so incremental uploads accumulate and a
re-uploaded/corrected file still supersedes its own dates.
"""
import asyncio
import io

import pytest
import openpyxl
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, User, EvalueAccount, EvalueBankTxn
from routes.evalue import upload_bank

USER = User(id="u1", username="raj", role="admin", permissions="{}")
ACCT = "ICIC-8580"


class _UploadFileStub:
    def __init__(self, content: bytes, filename: str):
        self._c = content
        self.filename = filename

    async def read(self):
        return self._c


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    s.add(EvalueAccount(bank_name="ICICI BANK", account_number="000705048580", reco_acc_no=ACCT))
    s.commit()
    yield s
    s.close()


def _icici_file(date_ddmon: str, remark: str, withdrawal: str, balance: str) -> bytes:
    """One-row ICICI 'Detailed Statement' workbook for a given date."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet0"
    for r in [
        ["Detailed Statement"],
        ["A/C No:", "000705048580"],
        [],
        ["S.N.", "Tran. Id", "Value Date", "Transaction Date", "Transaction Posted Date",
         "Cheque. No./Ref. No.", "Transaction Remarks", "Withdrawal Amt (INR)",
         "Deposit Amt (INR)", "Balance (INR)"],
        ["1", "S51619732", date_ddmon, date_ddmon, f"{date_ddmon} 10:17:56 AM", "",
         remark, withdrawal, "", balance],
    ]:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _upload(db, content, fname="stmt.xls"):
    return asyncio.run(upload_bank(
        file=_UploadFileStub(content, fname),
        bank_name="ICICI BANK", reco_acc_no=ACCT, db=db, user=USER))


def _dates(db):
    return sorted({r.txn_date for r in db.query(EvalueBankTxn)
                   .filter(EvalueBankTxn.reco_acc_no == ACCT).all()})


def test_incremental_daily_uploads_accumulate(db):
    _upload(db, _icici_file("01/Jul/2026", "IMPS/A/day1", "1000.00", "5000.00"), "d1.xls")
    assert _dates(db) == ["2026-07-01"]

    # a DIFFERENT day's file must NOT wipe the first day
    _upload(db, _icici_file("02/Jul/2026", "IMPS/B/day2", "2000.00", "3000.00"), "d2.xls")
    assert _dates(db) == ["2026-07-01", "2026-07-02"]

    _upload(db, _icici_file("03/Jul/2026", "IMPS/C/day3", "500.00", "2500.00"), "d3.xls")
    assert _dates(db) == ["2026-07-01", "2026-07-02", "2026-07-03"]


def test_reupload_same_date_supersedes_only_that_date(db):
    _upload(db, _icici_file("01/Jul/2026", "IMPS/A/orig", "1000.00", "5000.00"), "d1.xls")
    _upload(db, _icici_file("02/Jul/2026", "IMPS/B/day2", "2000.00", "3000.00"), "d2.xls")

    # correcting Jul 1 replaces ONLY Jul 1, leaves Jul 2 intact
    _upload(db, _icici_file("01/Jul/2026", "IMPS/A/CORRECTED", "1500.00", "4500.00"), "d1b.xls")
    assert _dates(db) == ["2026-07-01", "2026-07-02"]
    jul1 = [r for r in db.query(EvalueBankTxn).filter(EvalueBankTxn.reco_acc_no == ACCT).all()
            if r.txn_date == "2026-07-01"]
    assert len(jul1) == 1 and jul1[0].amount == 1500.0        # superseded, not duplicated
    assert "CORRECTED" in (jul1[0].description or "")


def test_cumulative_file_replaces_its_own_dates(db):
    # a 2-day cumulative file, then a 3-day cumulative for an overlapping+new range
    def multi(rows):
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Sheet0"
        ws.append(["Detailed Statement"]); ws.append(["A/C No:", "000705048580"]); ws.append([])
        ws.append(["S.N.", "Tran. Id", "Value Date", "Transaction Date", "Transaction Posted Date",
                   "Cheque. No./Ref. No.", "Transaction Remarks", "Withdrawal Amt (INR)",
                   "Deposit Amt (INR)", "Balance (INR)"])
        for i, (d, amt, bal) in enumerate(rows, 1):
            ws.append([str(i), f"S{i}", d, d, f"{d} 10:00 AM", "", f"IMPS/{i}", amt, "", bal])
        buf = io.BytesIO(); wb.save(buf); return buf.getvalue()

    _upload(db, multi([("04/Jul/2026", "100.00", "900.00"), ("05/Jul/2026", "200.00", "700.00")]), "c1.xls")
    assert _dates(db) == ["2026-07-04", "2026-07-05"]
    _upload(db, multi([("05/Jul/2026", "250.00", "650.00"), ("06/Jul/2026", "300.00", "350.00")]), "c2.xls")
    # Jul 4 preserved (not in 2nd file), Jul 5 superseded, Jul 6 added
    assert _dates(db) == ["2026-07-04", "2026-07-05", "2026-07-06"]
    jul5 = [r for r in db.query(EvalueBankTxn).filter(EvalueBankTxn.reco_acc_no == ACCT).all()
            if r.txn_date == "2026-07-05"]
    assert len(jul5) == 1 and jul5[0].amount == 250.0
