"""Unit tests for the dashboard result cache (core/dash_cache.py)."""
import core.dash_cache as dc


class _Bind:
    pass


class _DB:
    def __init__(self, bind):
        self._b = bind

    def get_bind(self):
        return self._b


def setup_function(_):
    dc.clear_dash_cache()


def test_put_returns_value_and_get_roundtrips():
    db = _DB(_Bind())
    k = dc.dash_key("panel", db, a=1, b=2)
    assert dc.dash_get(k) is None                 # miss before put
    assert dc.dash_put(k, {"v": 1}) == {"v": 1}   # put returns the value
    assert dc.dash_get(k) == {"v": 1}             # hit after put


def test_key_varies_by_params_and_bind_but_not_user():
    db1, db2 = _DB(_Bind()), _DB(_Bind())
    assert dc.dash_key("panel", db1, a=1) == dc.dash_key("panel", db1, a=1)   # stable
    assert dc.dash_key("panel", db1, a=1) != dc.dash_key("panel", db1, a=2)   # params matter
    assert dc.dash_key("panel", db1, a=1) != dc.dash_key("panel", db2, a=1)   # DB bind matters
    assert dc.dash_key("p1", db1, a=1)   != dc.dash_key("p2", db1, a=1)       # endpoint name matters
    # param order does not matter (sorted internally)
    assert dc.dash_key("panel", db1, a=1, b=2) == dc.dash_key("panel", db1, b=2, a=1)


def test_entry_expires_after_ttl(monkeypatch):
    db = _DB(_Bind())
    clock = [1000.0]
    monkeypatch.setattr(dc._time, "time", lambda: clock[0])
    monkeypatch.setattr(dc, "_TTL", 60.0)
    k = dc.dash_key("panel", db, a=1)
    dc.dash_put(k, "v")
    assert dc.dash_get(k) == "v"        # fresh
    clock[0] += 61                      # advance past TTL
    assert dc.dash_get(k) is None       # expired


def test_clear_empties_cache():
    db = _DB(_Bind())
    k = dc.dash_key("panel", db, a=1)
    dc.dash_put(k, "v")
    dc.clear_dash_cache()
    assert dc.dash_get(k) is None


def test_cap_is_enforced():
    db = _DB(_Bind())
    for i in range(dc._CAP + 60):
        dc.dash_put(dc.dash_key("panel", db, i=i), i)
    assert len(dc._CACHE) <= dc._CAP + 1   # never grows unbounded


def test_bust_clears_cache_and_never_raises():
    db = _DB(_Bind())
    k = dc.dash_key("panel", db, a=1)
    dc.dash_put(k, "v")
    dc.bust()                        # clears the dash cache (+ best-effort the analytics cache)
    assert dc.dash_get(k) is None
    dc.bust()                        # idempotent / safe to over-call
