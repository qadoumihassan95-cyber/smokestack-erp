"""Streaming gateway — permission-aware live event delivery with polling fallback.

The event source is the durable outbox (already the backbone), so streaming reuses one source of
truth. Two transports share ONE filtering path:
  * SSE (`event_stream`) for live push.
  * Poll (`poll`) for fallback / recovery via Last-Event-ID.

Guarantees (SUPER_ACCOUNT_WORKSPACE §2/§6):
  * ABAC-filtered per operator (pre-computed subscription filter; not a per-event PDP call).
  * Live revocation: the operator's session is re-checked each batch; a revoked/expired session
    stops the stream mid-connection.
  * Critical-event priority + backpressure: when a batch exceeds the cap, low-severity events are
    shed first; critical (security/incident/deploy-failure) events are never dropped.
  * Recovery: Last-Event-ID resumes from the last delivered id.
"""
from __future__ import annotations

import asyncio
import datetime
import json

import iam
import models
import sessions

# Event types considered critical — never shed under backpressure.
CRITICAL_PREFIXES = ("security.", "incident.", "breakglass.", "deploy.failed", "backup.failed",
                     "command.failed")


def subscription_filter(op) -> dict:
    """Pre-compute an operator's allowed event view from their ABAC scope (evaluated once per
    connection / on scope change), rather than running the PDP per event at fleet rate."""
    scope = iam.operator_scopes(op)
    return {"erp": scope.get("erp"), "region": scope.get("region"),
            "env": scope.get("env"), "customer": scope.get("customer")}


def _scope_allows(sub: dict, dim: str, value) -> bool:
    allowed = sub.get(dim)
    if allowed is None or "*" in allowed:
        return True
    return value is None or str(value) in [str(a) for a in allowed]


def _visible(sub: dict, event: "models.Outbox") -> bool:
    try:
        p = json.loads(event.payload) if event.payload else {}
    except Exception:
        p = {}
    for dim, key in (("erp", "erp_product_id"), ("region", "region"),
                     ("env", "environment"), ("customer", "customer_ref")):
        if key in p and not _scope_allows(sub, dim, p.get(key)):
            return False
    return True


def _is_critical(evt_type: str) -> bool:
    return any(evt_type.startswith(p) for p in CRITICAL_PREFIXES)


def fetch(db, op, *, since_id=0, limit=100) -> dict:
    """Permission-filtered batch of events after `since_id`. Applies backpressure with critical
    priority when more than `limit` are visible."""
    sub = subscription_filter(op)
    rows = (db.query(models.Outbox).filter(models.Outbox.id > since_id)
            .order_by(models.Outbox.id.asc()).limit(limit * 5).all())
    visible = [r for r in rows if _visible(sub, r)]
    shed = 0
    if len(visible) > limit:
        crit = [r for r in visible if _is_critical(r.event_type)]
        non = [r for r in visible if not _is_critical(r.event_type)]
        keep = crit + non[: max(0, limit - len(crit))]
        keep.sort(key=lambda r: r.id)
        shed = len(visible) - len(keep)
        visible = keep
    last_id = visible[-1].id if visible else since_id
    return {"events": [{"id": r.id, "type": r.event_type, "correlation_id": r.correlation_id,
                        "at": (r.created_at.isoformat() if r.created_at else None),
                        "payload": (json.loads(r.payload) if r.payload else {})} for r in visible],
            "last_event_id": last_id, "shed": shed}


def poll(db, op, *, since_id=0, limit=100) -> dict:
    """Polling fallback. Same filtering as the live stream."""
    return fetch(db, op, since_id=since_id, limit=limit)


async def event_stream(db_factory, op, jti, *, since_id=0, interval=1.0, max_batches=None):
    """SSE generator. Re-checks the operator's session each batch (live revocation) and yields
    Server-Sent-Events. `db_factory` returns a fresh Session per batch (never hold one open)."""
    last = since_id
    batches = 0
    while True:
        db = db_factory()
        try:
            if not sessions.is_active(db, jti):     # live revocation mid-stream
                yield "event: session_revoked\ndata: {}\n\n"
                return
            sessions.touch(db, jti)
            batch = fetch(db, op, since_id=last, limit=100)
        finally:
            db.close()
        for e in batch["events"]:
            last = e["id"]
            yield f"id: {e['id']}\nevent: {e['type']}\ndata: {json.dumps(e)}\n\n"
        yield f": keepalive {datetime.datetime.utcnow().isoformat()}\n\n"
        batches += 1
        if max_batches is not None and batches >= max_batches:
            return
        await asyncio.sleep(interval)
