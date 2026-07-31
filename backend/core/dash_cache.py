# ── Dashboard result cache ────────────────────────────────────────────────────
# Short-TTL cache for the read-only Dashboard panels (dashboard-summary, the E-Value /
# BBPS / SBI-Kiosk summaries, funds-position, pg-settlement-summary). Each is a wide
# GROUP-BY / aggregate over large tables; the underlying data only changes when a
# recon/upload runs (a few times a day), so caching the response for a few seconds turns
# repeated dashboard loads and tab-switches into a dict hit instead of a fresh scan.
#
# This mirrors the analytics cache in core/analytics.py exactly (same philosophy, same
# TTL default, same DB-bind-id namespacing so unit tests with their own in-memory engine
# never share results). Single-worker process → a module dict is safe. It is read-only:
# no reconciliation engine ever consults it, and a stale entry self-heals within the TTL.
import os as _os
import time as _time

# TTL default 60s; honours DASH_CACHE_TTL, falling back to ANALYTICS_CACHE_TTL so both
# caches can be tuned together with one env var.
_TTL = float(_os.getenv("DASH_CACHE_TTL", _os.getenv("ANALYTICS_CACHE_TTL", "60")))
_CACHE = {}    # key -> (expires_at, value)
_CAP = 256


def _bind_id(db):
    try:
        return id(db.get_bind())
    except Exception:
        return 0


def dash_key(name, db, **params):
    """Cache key = (endpoint name, DB bind id, sorted params). The result of these
    endpoints does not depend on the calling user (no per-user scoping), so the key
    deliberately omits the user — every authenticated user shares the same aggregate."""
    return (name, _bind_id(db)) + tuple(sorted(params.items()))


def dash_get(key):
    hit = _CACHE.get(key)
    if hit and hit[0] > _time.time():
        return hit[1]
    return None


def dash_put(key, val):
    now = _time.time()
    if len(_CACHE) > _CAP:                       # prune expired, then hard-cap
        for k in [k for k, v in _CACHE.items() if v[0] <= now]:
            _CACHE.pop(k, None)
        if len(_CACHE) > _CAP:
            _CACHE.clear()
    _CACHE[key] = (now + _TTL, val)
    return val                                   # so callers can `return dash_put(k, result)`


def clear_dash_cache():
    """Invalidate the whole dashboard cache (e.g. after a recon/upload) so panels update now."""
    _CACHE.clear()


def bust():
    """Invalidate BOTH short-TTL reporting caches (this dashboard cache AND the exec
    analytics cache) after any write that changes reconciliation data, so the Dashboard,
    Analytics and the digest recompute on the next read instead of serving a pre-write
    aggregate. Cheap (two dict clears) and safe to over-call — the caches are read-only and
    self-heal within the TTL, so an extra bust only costs one recompute. NEVER raises."""
    try:
        _CACHE.clear()
    except Exception:
        pass
    try:
        from core.analytics import clear_analytics_cache
        clear_analytics_cache()
    except Exception:
        pass
