"""
SBI kiosk auto-recon on upload (parity with the core products' post-upload chain).

Invariants:
1. Every SBI upload endpoint triggers P01→P04 for today's recon_date and reports
   the outcome under 'auto_recon' in its response.
2. A failing process NEVER blocks the upload — errors are swallowed per-process
   (missing counterpart files mid-day are normal; the next upload completes them).
3. Results land under today's recon_date (upload_date≈recon_date, contract #17).
"""
import asyncio
import datetime
import io

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, User, SBIBankTransaction, SBIP02Result
from routes.sbi_kiosk import upload_bank_statement, _auto_run_after_upload

USER = User(id="u1", username="raj", role="admin", permissions="{}")
TODAY = str(datetime.date.today())


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
    yield s
    s.close()


def _bank_stmt() -> bytes:
    lines = [
        "Some header noise",
        "Txn Date\tValue Date\tDescription\tRef No./Cheque No.\tBranch Code\tDebit\tCredit\tBalance",
        f"01/07/2026\t01/07/2026\tTO TRANSFER EKOSETTLEMENT KO123 01072026\tREF1\t99922\t\t5000\t105000",
        f"01/07/2026\t01/07/2026\tMoneyTRF something\tREF2\t99922\t2000\t\t103000",
    ]
    return "\n".join(lines).encode()


def test_upload_triggers_auto_recon_and_never_blocks(db):
    res = asyncio.run(upload_bank_statement(
        file=_UploadFileStub(_bank_stmt(), "stmt.xls"), recon_date="",
        db=db, current_user=USER))
    assert res["inserted"] == 2
    # auto_recon key present with a per-process outcome for today's recon date
    ar = res["auto_recon"]
    assert ar["recon_date"] == TODAY
    assert set(ar) >= {"recon_date", "p01", "p02", "p03", "p04"}
    # every process either ran or was skipped — none may raise out of the upload
    for p in ("p01", "p02", "p03", "p04"):
        assert ar[p] == "ok" or ar[p].startswith("skipped:")
    # P02 ran against the uploaded statement → results exist for today's recon_date
    if ar["p02"] == "ok":
        assert db.query(SBIP02Result).filter(SBIP02Result.recon_date == TODAY).count() > 0
    # the upload itself is committed regardless of process outcomes
    assert db.query(SBIBankTransaction).count() == 2


def test_auto_run_swallows_every_process_failure(db, monkeypatch):
    import routes.sbi_kiosk as SK

    def _boom(**kwargs):
        raise RuntimeError("engine exploded")

    for p in ("run_p01", "run_p02", "run_p03", "run_p04"):
        monkeypatch.setattr(SK, p, _boom)
    out = _auto_run_after_upload(db, USER)     # must not raise
    assert all(out[p].startswith("skipped:") for p in ("p01", "p02", "p03", "p04"))
