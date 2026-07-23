"""
_clean must strip Excel text-format apostrophes from ingested values.

[Live incident 2026-07-22: the AePS internal dump's id columns were exported
with leading apostrophes ('3570175846), matching found zero overlap with the
clean bank-side ids, and the whole day (135 pairs, all within tolerance) sat
unmatched. _clean is shared by BOTH ingest copies (routes/upload.py and
core/ingest_service.py imports it), so this one behavior covers both.]
"""
from routes.upload import _clean


def test_leading_apostrophe_stripped():
    assert _clean("'3570175846") == "3570175846"
    assert _clean(" '620312737815 ") == "620312737815"
    assert _clean("''123") == "123"          # double text-marker


def test_interior_apostrophe_untouched():
    assert _clean("O'Brien Stores") == "O'Brien Stores"


def test_apostrophe_wrapped_null_markers_still_null():
    assert _clean("'") is None
    assert _clean("'N/A") is None
    assert _clean("") is None
    assert _clean(None) is None


def test_plain_values_unchanged():
    assert _clean("3570194429") == "3570194429"
    assert _clean("  Success  ") == "Success"


# ── every module engine's own cleaner must strip the marker too ────────────────
# (2026-07-23 sweep: SBI P01–P03 match on RAW equality; E-Value's Success/DR
# eligibility filter and both upsert identities read raw; AePS/QR settlement
# rrn cross-refs and dedup keys read raw. Live data was clean — these are the
# preventive guards so the AePS 22-07 incident can't recur via any ingest path.)
def test_sbi_clean_strips_marker():
    from routes.sbi_kiosk import _clean as sbi_clean
    assert sbi_clean("'61960000000000000001") == "61960000000000000001"
    assert sbi_clean("nan") == ""


def test_evalue_clean_strips_marker():
    from core.evalue_engine import _clean as ev_clean
    assert ev_clean("'Success") == "Success"
    assert ev_clean("'DR") == "DR"
    assert ev_clean("'-") == ""          # apostrophe-marked null stays null


def test_bbps_clean_strips_marker():
    from core.bbps_engine import _clean as bbps_clean
    assert bbps_clean("'Failed") == "Failed"
    assert bbps_clean("-") == ""


def test_aeps_settlement_noq_strips_marker():
    from routes.aeps_settlement import _noq
    assert _noq("'520512345678") == "520512345678"
    assert _noq("520512345678") == "520512345678"
    assert _noq(None) == ""
