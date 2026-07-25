"""Event backbone — transactional outbox with at-least-once relay, retry, dead-letter, replay.

Minimum production-grade infrastructure, additive and broker-agnostic: events are written to the
`outbox` table in the SAME transaction as the state change (no dual-write bug). A relay drains
pending events to registered in-process handlers with retry and a terminal dead-letter state.
Because the outbox is the durable source of truth, a real broker (Kafka/SQS/…) can later consume
it with no rewrite of producers. Consumers must be idempotent (events carry a stable dedupe_key).
"""
from __future__ import annotations

import datetime
import json

import models

# event_type -> list[handler(db, event_row) -> None]. Handlers raise to signal retryable failure.
_HANDLERS: dict[str, list] = {}


def subscribe(event_type: str):
    def _wrap(fn):
        _HANDLERS.setdefault(event_type, []).append(fn)
        return fn
    return _wrap


def emit(db, *, aggregate_type, aggregate_id, event_type, payload, correlation_id=None,
         causation_id=None, dedupe_key=None, event_version=1, commit=False):
    """Append an event to the outbox. MUST be called inside the state-change transaction
    (commit=False) so the event and the change commit atomically."""
    key = dedupe_key or f"{event_type}:{aggregate_type}:{aggregate_id}:{correlation_id}"
    # idempotent producer: a duplicate dedupe_key is a no-op
    if db.query(models.Outbox).filter_by(dedupe_key=key).first():
        return None
    row = models.Outbox(
        aggregate_type=aggregate_type, aggregate_id=str(aggregate_id), event_type=event_type,
        event_version=event_version, payload=json.dumps(payload), correlation_id=correlation_id,
        causation_id=causation_id, dedupe_key=key, status="pending", attempts=0)
    db.add(row)
    if commit:
        db.commit()
    else:
        db.flush()
    return row


def _dispatch(db, row) -> None:
    for h in _HANDLERS.get(row.event_type, []):
        h(db, row)


def relay(db, limit: int = 100) -> dict:
    """Drain due pending/failed events to handlers. At-least-once; idempotent consumers required.
    Returns a summary. Safe to call repeatedly (a scheduled worker or on-demand endpoint)."""
    now = datetime.datetime.utcnow()
    published = failed = dead = 0
    rows = (db.query(models.Outbox)
            .filter(models.Outbox.status.in_(["pending", "failed"]))
            .order_by(models.Outbox.id.asc()).limit(limit).all())
    for row in rows:
        av = row.available_at
        if av is not None and av.tzinfo is not None:
            av = av.replace(tzinfo=None)
        if av is not None and av > now:
            continue  # backoff not elapsed
        try:
            _dispatch(db, row)
            row.status = "published"
            row.published_at = now
            row.last_error = None
            published += 1
            db.commit()
        except Exception as e:            # retryable failure → backoff, or dead-letter
            db.rollback()
            row = db.get(models.Outbox, row.id)
            row.attempts = (row.attempts or 0) + 1
            row.last_error = f"{type(e).__name__}: {e}"[:500]
            if row.attempts >= (row.max_attempts or 5):
                row.status = "dead"       # dead-letter terminal state
                dead += 1
            else:
                row.status = "failed"
                backoff = min(2 ** row.attempts, 3600)
                row.available_at = now + datetime.timedelta(seconds=backoff)
                failed += 1
            db.commit()
    return {"published": published, "retry_scheduled": failed, "dead_lettered": dead}


def replay(db, *, event_type: str | None = None, since_id: int = 0, limit: int = 1000) -> int:
    """Re-dispatch already-published events to handlers (e.g. to rebuild a projection). Does not
    change outbox status. Returns count dispatched."""
    q = db.query(models.Outbox).filter(models.Outbox.id > since_id)
    if event_type:
        q = q.filter(models.Outbox.event_type == event_type)
    n = 0
    for row in q.order_by(models.Outbox.id.asc()).limit(limit).all():
        _dispatch(db, row)
        n += 1
    db.commit()
    return n


def dead_letters(db, limit: int = 100):
    return (db.query(models.Outbox).filter_by(status="dead")
            .order_by(models.Outbox.id.desc()).limit(limit).all())
