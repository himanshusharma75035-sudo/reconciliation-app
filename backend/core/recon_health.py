"""
core/recon_health.py

Recon-health watchdog (D2 — additive, read-only).

compute_recon_health() aggregates failure signals the dead EOD digest never
surfaced into one structured health report:
  * failed ingests          (1.4 ingestion ledger, status='failed')
  * blocked re-uploads      (1.4 ledger, status='blocked' — the double-count guard)
  * watch-folder errors     (WatchFolderConfig.last_trigger_status error/not_found)
  * data-quality warnings   (1.6 dq_profile.has_warnings)
  * low recon match rate     (recent ReconRun aggregates)

READ ONLY — it never writes, never runs reconciliation, and every individual
check is wrapped so one failing query can never break the report or raise into a
caller. Severity ranks: critical > warn > ok; 'unknown' marks a check that errored.
"""
import json
import datetime
import logging

logger = logging.getLogger("eko_recon.recon_health")

_RANK = {"ok": 0, "unknown": 1, "warn": 2, "critical": 3}

# Tunable thresholds (display-only).
_MATCH_RATE_WARN = 0.50      # recent aggregate match rate below this → warn


def _check(key, label, fn):
    """Run one check fn() -> (severity, message, detail); never raises."""
    try:
        severity, message, detail = fn()
    except Exception as e:
        severity, message, detail = "unknown", f"check failed: {e}", None
    return {"key": key, "label": label, "severity": severity,
            "ok": severity == "ok", "message": message, "detail": detail}


def compute_recon_health(db, days: int = 7) -> dict:
    """Return a structured, read-only recon-health report over the last `days`."""
    from models.database import IngestionEvent, WatchFolderConfig
    since = datetime.datetime.utcnow() - datetime.timedelta(days=days)

    def _failed_ingests():
        n = db.query(IngestionEvent).filter(
            IngestionEvent.created_at >= since,
            IngestionEvent.status == "failed").count()
        if n == 0:
            return "ok", "No failed ingests", {"count": 0}
        return "warn", f"{n} failed ingest(s) in {days}d", {"count": n}

    def _blocked_ingests():
        n = db.query(IngestionEvent).filter(
            IngestionEvent.created_at >= since,
            IngestionEvent.status == "blocked").count()
        if n == 0:
            return "ok", "No blocked re-uploads", {"count": 0}
        # Blocked = the duplicate-slot guard fired; informational, not a failure.
        return "warn", f"{n} blocked re-upload attempt(s) in {days}d", {"count": n}

    def _watch_folders():
        rows = db.query(WatchFolderConfig).all()
        bad = [{"label": w.label, "status": w.last_trigger_status,
                "message": w.last_trigger_message}
               for w in rows if (w.last_trigger_status or "") in ("error", "not_found")]
        if not bad:
            return "ok", "Watch folders healthy", {"errors": 0}
        sev = "critical" if any(b["status"] == "error" for b in bad) else "warn"
        return sev, f"{len(bad)} watch folder(s) in error/not_found", {"folders": bad}

    def _dq_warnings():
        rows = db.query(IngestionEvent).filter(
            IngestionEvent.created_at >= since,
            IngestionEvent.dq_profile.isnot(None)).all()
        n = 0
        for e in rows:
            try:
                if json.loads(e.dq_profile).get("has_warnings"):
                    n += 1
            except Exception:
                continue
        if n == 0:
            return "ok", "No data-quality warnings", {"count": 0}
        return "warn", f"{n} ingest(s) with data-quality warnings in {days}d", {"count": n}

    def _match_rate():
        # Compute over the CURRENT state of recently-dated txn rows, NOT ReconRun
        # logs: ReconRun only records same-date run_reconciliation, so D+1 (QR),
        # reversal, and internal-self matches it never sees read as 0% and would
        # false-warn. Open = the Open-Items default {unmatched, src_assigned}
        # (behavior-contract #14); everything else (matched / reversal_matched /
        # fee_matched / duplicate / …) counts as resolved.
        from models.database import Transaction, ReconStatus
        cutoff = (datetime.datetime.utcnow().date()
                  - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        base = db.query(Transaction).filter(
            Transaction.row_type == "txn",
            Transaction.recon_date.like("____-__-__"),   # real dates only, skip 'auto'
            Transaction.recon_date >= cutoff,
        )
        core_total = base.count()
        core_open = base.filter(Transaction.recon_status.in_(
            [ReconStatus.unmatched, ReconStatus.src_assigned])).count()
        # Assess EACH product separately, not one blended rate — the module products
        # (E-Value / BBPS / SBI Kiosk) reconcile in their own tables, and lumping them
        # into a single average lets a break in one hide behind another (e.g. SBI Kiosk's
        # high-volume structural rate would swamp a core-ledger collapse, or vice-versa).
        # build_analytics unions the module tables and buckets each product's vocabulary;
        # its 'unmatched' bucket is the chase-worthy open count (parity with the core
        # {unmatched, src_assigned}). Core semantics above are left untouched.
        buckets = []   # (label, total, open) — one per product with data
        if core_total:
            buckets.append(("Core ledger", core_total, core_open))
        try:
            from core.analytics import build_analytics
            agg = build_analytics(db, date_from=cutoff)
            for g in agg.get("by_group", []):
                if g.get("group") in ("evalue", "bbps", "kiosk"):
                    t, op = g.get("transactions", 0) or 0, g.get("unmatched", 0) or 0
                    if t:
                        buckets.append((g.get("label") or g["group"], t, op))
        except Exception:
            logger.warning("recon_health: module aggregation skipped", exc_info=True)

        total = sum(t for _, t, _ in buckets)
        open_n = sum(o for _, _, o in buckets)
        if total < 50:   # volume guard — don't warn on a handful of rows
            return "ok", f"Too few recent txns to assess ({total})", {"total": total}
        rate = round((total - open_n) / total, 4)
        # Per-product rates so the payload shows WHICH product is behind, not just a blend.
        per = [{"product": lbl, "total": t, "open": o, "rate": round((t - o) / t, 4)}
               for lbl, t, o in buckets if t]
        detail = {"total": total, "open": open_n, "resolved": total - open_n,
                  "rate": rate, "by_product": per}
        # Warn on the worst single product with enough volume to judge (>=200 rows) —
        # this catches a one-product break the blended rate would mask. Falls back to the
        # blended rate when no single product qualifies.
        low = [p for p in per if p["total"] >= 200 and p["rate"] < _MATCH_RATE_WARN]
        if low:
            w = min(low, key=lambda p: p["rate"])
            return "warn", f"{w['product']} only {round(w['rate'] * 100)}% reconciled ({w['open']} open)", detail
        if rate < _MATCH_RATE_WARN:
            return "warn", f"Only {round(rate * 100)}% of recent txns reconciled ({open_n} open)", detail
        return "ok", f"{round(rate * 100)}% of recent txns reconciled", detail

    checks = [
        _check("failed_ingests",  "Failed ingests",       _failed_ingests),
        _check("blocked_ingests", "Blocked re-uploads",   _blocked_ingests),
        _check("watch_folders",   "Watch folders",        _watch_folders),
        _check("dq_warnings",     "Data-quality warnings", _dq_warnings),
        _check("match_rate",      "Reconciliation rate",  _match_rate),
    ]
    status = max((c["severity"] for c in checks), key=lambda s: _RANK.get(s, 0))
    return {"status": status, "window_days": days, "checks": checks}
