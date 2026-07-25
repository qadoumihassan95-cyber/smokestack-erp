"""Tamper-evident, hash-chained platform audit (Mission Control foundation).

Every appended audit row commits to the previous row's hash:
    entry_hash = sha256(prev_hash + canonical(row_fields))
Any later deletion or edit of history breaks the chain and is detected by `verify()`. This upgrades
the existing `platform_audit_log` additively — legacy rows (null chain fields) are the genesis.

The chain is append-only by construction here; DB-level WORM/immutability is a later hardening step.
"""
from __future__ import annotations

import datetime
import hashlib
import json

import models

GENESIS = "0" * 64


def _canonical(row: dict) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)


def _row_fields(a: "models.PlatformAuditLog") -> dict:
    return {"actor": a.actor_operator_id, "action": a.action, "target_type": a.target_type,
            "target_id": a.target_id, "detail": a.detail, "result": a.result,
            "correlation_id": a.correlation_id, "command_type": a.command_type,
            "idempotency_key": a.idempotency_key, "at": (a.at.isoformat() if a.at else None)}


def _compute(prev_hash: str, fields: dict) -> str:
    return hashlib.sha256((prev_hash + _canonical(fields)).encode()).hexdigest()


def _last(db):
    """Last *chained* row (ignores interleaved legacy/unchained audit writes), so chained
    entries always link to each other regardless of other audit traffic."""
    return (db.query(models.PlatformAuditLog)
            .filter(models.PlatformAuditLog.entry_hash.isnot(None))
            .order_by(models.PlatformAuditLog.id.desc()).first())


def append(db, *, actor_operator_id=None, action="", target_type="", target_id="",
           detail="", result="ok", correlation_id=None, command_type=None,
           idempotency_key=None, commit=True) -> "models.PlatformAuditLog":
    """Append one hash-chained audit entry. Caller owns the surrounding transaction unless commit."""
    prev = _last(db)
    prev_hash = (prev.entry_hash if prev and prev.entry_hash else GENESIS)
    now = datetime.datetime.utcnow()
    entry = models.PlatformAuditLog(
        actor_operator_id=actor_operator_id, action=action, target_type=target_type,
        target_id=str(target_id), detail=detail, result=result, correlation_id=correlation_id,
        command_type=command_type, idempotency_key=idempotency_key, prev_hash=prev_hash, at=now)
    entry.entry_hash = _compute(prev_hash, _row_fields(entry))
    db.add(entry)
    if commit:
        db.commit()
    else:
        db.flush()
    return entry


def verify(db) -> tuple[bool, str]:
    """Re-walk the chain. Returns (ok, detail). Rows before hash-chaining (null entry_hash) are
    skipped as pre-chain genesis; the first hashed row anchors on GENESIS or the prior hash."""
    rows = db.query(models.PlatformAuditLog).order_by(models.PlatformAuditLog.id.asc()).all()
    prev_hash = None
    for a in rows:
        if a.entry_hash is None:
            continue  # legacy pre-chain row
        expected_prev = prev_hash if prev_hash is not None else (a.prev_hash or GENESIS)
        if a.prev_hash != expected_prev:
            return False, f"broken_link_at_id_{a.id}"
        if _compute(a.prev_hash, _row_fields(a)) != a.entry_hash:
            return False, f"tampered_row_id_{a.id}"
        prev_hash = a.entry_hash
    return True, "chain_valid"
