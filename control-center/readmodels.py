"""CQRS read models — first projection: rm_command_feed.

A read model is a denormalized projection built asynchronously from the outbox event stream. It is
NOT a correctness gate (the command pipeline re-validates authoritative state at execution, M1);
it exists to make reads fast and decoupled. Every read model is rebuildable by replaying events,
carries a freshness watermark, and reports staleness.
"""
from __future__ import annotations

import datetime
import json

import events
import models

FEED = "rm_command_feed"


def _bump_watermark(db, event_id: int):
    st = db.get(models.ReadModelState, FEED)
    if st is None:
        st = models.ReadModelState(name=FEED, last_event_id=0)
        db.add(st)
    if event_id > (st.last_event_id or 0):
        st.last_event_id = event_id
    st.updated_at = datetime.datetime.utcnow()


@events.subscribe("command.completed")
@events.subscribe("command.rejected")
@events.subscribe("command.failed")
def project_command(db, row):
    """Idempotent upsert of one command event into the feed (keyed by outbox event id)."""
    p = json.loads(row.payload) if row.payload else {}
    existing = db.get(models.RMCommandFeed, row.id)
    if existing is None:
        db.add(models.RMCommandFeed(
            id=row.id, command_type=p.get("command_type"), operator_id=p.get("operator_id"),
            target=p.get("target"), status=p.get("status"), blast_radius=p.get("blast_radius"),
            correlation_id=row.correlation_id, occurred_at=row.created_at))
    _bump_watermark(db, row.id)


def freshness(db) -> dict:
    """Report projection lag: max outbox id vs the read model's watermark."""
    st = db.get(models.ReadModelState, FEED)
    max_id = db.query(models.Outbox.id).order_by(models.Outbox.id.desc()).limit(1).scalar() or 0
    wm = (st.last_event_id if st else 0) or 0
    return {"read_model": FEED, "watermark": wm, "max_event_id": max_id,
            "lag_events": max(0, max_id - wm), "status": (st.status if st else "uninitialized"),
            "updated_at": (st.updated_at.isoformat() if st and st.updated_at else None)}


def rebuild(db) -> dict:
    """Full deterministic rebuild: clear the projection, reset the watermark, replay every
    command.* event from the durable outbox. Proves the read model is derived, not a source."""
    st = db.get(models.ReadModelState, FEED)
    if st is None:
        st = models.ReadModelState(name=FEED)
        db.add(st)
    st.status = "rebuilding"
    db.query(models.RMCommandFeed).delete()
    st.last_event_id = 0
    db.commit()
    n = 0
    for et in ("command.completed", "command.rejected", "command.failed"):
        n += events.replay(db, event_type=et)
    st = db.get(models.ReadModelState, FEED)
    st.status = "live"
    st.rebuilt_at = datetime.datetime.utcnow()
    db.commit()
    return {"rebuilt": True, "events_replayed": n, "rows": db.query(models.RMCommandFeed).count()}


def validate(db) -> tuple[bool, str]:
    """Consistency check: every projected row must correspond to an outbox event id."""
    ids = {r.id for r in db.query(models.RMCommandFeed.id).all()}
    if not ids:
        return True, "empty"
    present = {r.id for r in db.query(models.Outbox.id).filter(models.Outbox.id.in_(ids)).all()}
    missing = ids - present
    return (len(missing) == 0), ("consistent" if not missing else f"orphans:{sorted(missing)[:5]}")
