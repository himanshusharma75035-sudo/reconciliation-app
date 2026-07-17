"""
core/recycle_bin.py — soft delete + restore.

Deleting reconciliation rows used to be terminal: a Clear action issued a hard DELETE and
the only way back was a nightly DB dump. This module makes a Clear *reversible* — rows are
serialised into `recycle_bin` first, then deleted, and can be restored later.

REPORTING/OPS ONLY: nothing here is read by a matching engine. It never changes a row's
recon state — a restore puts the original row back exactly as it was.

Design notes
------------
* One Clear action = one `batch_id`; each (table, chunk) becomes its own recycle_bin row so
  a 170k-row delete never builds one oversized blob (MySQL max_allowed_packet) or holds the
  whole result set in memory.
* Rows are read with yield_per and deleted BY PRIMARY KEY — so we only ever delete exactly
  what we captured, even if the query is non-deterministic.
* Values are stringified via a JSON default; restore feeds them back through the model
  constructor, which coerces them back (dates/decimals are stored as strings in this schema
  anyway — see the timezone/money conventions in CLAUDE.md).
* Restore is idempotent: rows whose primary key already exists are skipped, so a double
  restore can't duplicate live data.
"""
import gzip
import json
import logging
import datetime

from models.database import RecycleBin, generate_id

logger = logging.getLogger("eko_recon.recycle_bin")

CHUNK = 2000          # rows per recycle_bin payload
RETAIN_DAYS = 30      # purge_expired() default


def _ser(v):
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)


def _pack(rows) -> bytes:
    return gzip.compress(json.dumps(rows, default=_ser).encode("utf-8"))


def _unpack(blob) -> list:
    return json.loads(gzip.decompress(blob).decode("utf-8"))


def _deser_row(model, r: dict) -> dict:
    """JSON row → kwargs for the model constructor.

    _ser() flattens datetimes to ISO strings (JSON has no date type); DateTime/Date columns
    must be handed back real objects or SQLAlchemy rejects the insert ("SQLite DateTime type
    only accepts Python datetime and date objects"). Everything else (str/int/float/bool)
    round-trips natively.
    """
    from sqlalchemy import DateTime, Date

    out = {}
    for c in model.__table__.columns:
        if c.name not in r:
            continue
        v = r[c.name]
        if v is None:
            continue
        if isinstance(v, str):
            try:
                if isinstance(c.type, DateTime):
                    v = datetime.datetime.fromisoformat(v)
                elif isinstance(c.type, Date):
                    v = datetime.date.fromisoformat(v)
            except ValueError:
                pass          # leave as-is; the column is a plain string date (this schema
                              # stores business dates as 'YYYY-MM-DD' strings on purpose)
        out[c.name] = v
    return out


def soft_delete(db, query, model, *, module, user, filters=None, reason=None,
                batch_id=None, commit=False) -> int:
    """Capture `query`'s rows into the recycle bin, then delete them. Returns the count.

    The caller owns the transaction (commit=False) so a multi-table Clear either lands
    whole or not at all.
    """
    table = model.__tablename__
    pk = list(model.__table__.primary_key.columns)[0]
    cols = [c.name for c in model.__table__.columns]
    batch_id = batch_id or generate_id()

    buf, ids, total, chunk_no = [], [], 0, 0

    def _flush():
        nonlocal buf, chunk_no
        if not buf:
            return
        db.add(RecycleBin(
            id=generate_id(), batch_id=batch_id, module=module, table_name=table,
            chunk_no=chunk_no, row_count=len(buf), payload=_pack(buf),
            filters=json.dumps(filters or {}), reason=(reason or "")[:300],
            deleted_by=user, deleted_at=datetime.datetime.utcnow(),
        ))
        chunk_no += 1
        buf = []

    for row in query.yield_per(CHUNK):
        buf.append({c: getattr(row, c, None) for c in cols})
        ids.append(getattr(row, pk.name))
        total += 1
        if len(buf) >= CHUNK:
            _flush()
    _flush()

    # Delete exactly what we captured, by primary key, in batches.
    for i in range(0, len(ids), CHUNK):
        db.query(model).filter(pk.in_(ids[i:i + CHUNK])).delete(synchronize_session=False)

    if commit:
        db.commit()
    logger.info("recycle_bin: captured %d row(s) from %s (batch %s)", total, table, batch_id)
    return total


def restore_batch(db, batch_id: str, user: str) -> dict:
    """Re-insert every row captured under `batch_id`. Idempotent — existing PKs are skipped."""
    from models.database import Base

    bins = (db.query(RecycleBin)
              .filter(RecycleBin.batch_id == batch_id)
              .order_by(RecycleBin.table_name, RecycleBin.chunk_no).all())
    if not bins:
        raise ValueError(f"Nothing in the recycle bin for batch {batch_id}")

    models = {m.class_.__tablename__: m.class_ for m in Base.registry.mappers}
    restored, skipped = {}, {}

    for b in bins:
        model = models.get(b.table_name)
        if model is None:
            logger.warning("recycle_bin: unknown table %s — skipped", b.table_name)
            continue
        pk = list(model.__table__.primary_key.columns)[0]
        rows = _unpack(b.payload)
        keys = [r.get(pk.name) for r in rows]
        existing = {k for (k,) in db.query(pk).filter(pk.in_(keys)).all()} if keys else set()
        for r in rows:
            if r.get(pk.name) in existing:
                skipped[b.table_name] = skipped.get(b.table_name, 0) + 1
                continue
            db.add(model(**_deser_row(model, r)))
            restored[b.table_name] = restored.get(b.table_name, 0) + 1
        b.restored_at = datetime.datetime.utcnow()
        b.restored_by = user

    db.commit()
    logger.info("recycle_bin: restored batch %s -> %s (skipped %s)", batch_id, restored, skipped)
    return {"batch_id": batch_id, "restored": restored, "skipped_existing": skipped}


def list_batches(db, limit: int = 100, include_restored: bool = True) -> list:
    """One entry per Clear action, newest first (chunks/tables rolled up)."""
    q = db.query(RecycleBin).order_by(RecycleBin.deleted_at.desc())
    if not include_restored:
        q = q.filter(RecycleBin.restored_at.is_(None))
    out = {}
    for b in q.limit(limit * 40).all():
        e = out.setdefault(b.batch_id, {
            "batch_id": b.batch_id, "module": b.module, "deleted_by": b.deleted_by,
            "deleted_at": b.deleted_at, "reason": b.reason, "filters": b.filters,
            "restored_at": b.restored_at, "restored_by": b.restored_by,
            "tables": {}, "total_rows": 0,
        })
        e["tables"][b.table_name] = e["tables"].get(b.table_name, 0) + (b.row_count or 0)
        e["total_rows"] += (b.row_count or 0)
    return sorted(out.values(), key=lambda x: x["deleted_at"] or datetime.datetime.min,
                  reverse=True)[:limit]


def purge_batch(db, batch_id: str) -> int:
    """Permanently drop a batch from the bin (the rows themselves are already gone)."""
    n = db.query(RecycleBin).filter(RecycleBin.batch_id == batch_id).delete(synchronize_session=False)
    db.commit()
    return n


def purge_expired(db, days: int = RETAIN_DAYS) -> int:
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    n = db.query(RecycleBin).filter(RecycleBin.deleted_at < cutoff).delete(synchronize_session=False)
    db.commit()
    if n:
        logger.info("recycle_bin: purged %d expired row(s) older than %dd", n, days)
    return n
