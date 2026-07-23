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
