"""Operator sessions — opened at login, revocable, expiring; track MFA + break-glass + presence.

A session binds to the JWT's `jti`. Revoking a session invalidates the bound token even before it
expires (checked in the auth path). Presence powers "another operator is here" in the workspace.
"""
from __future__ import annotations

import datetime

import models


def open_session(db, operator, jti, *, device=None, ip=None, ttl_minutes=720, commit=True):
    s = models.OperatorSession(
        operator_id=operator.id, jti=jti, device=device, ip=ip,
        mfa_state=("satisfied" if getattr(operator, "mfa_enabled", False) else "none"),
        break_glass=False, status="active",
        expires_at=datetime.datetime.utcnow() + datetime.timedelta(minutes=ttl_minutes))
    db.add(s)
    if commit:
        db.commit()
    return s


def _effective_status(s) -> str:
    if s.status != "active":
        return s.status
    exp = s.expires_at
    if exp is not None:
        now = datetime.datetime.now(exp.tzinfo) if exp.tzinfo else datetime.datetime.utcnow()
        if now >= exp:
            return "expired"
    return "active"


def is_active(db, jti) -> bool:
    if not jti:
        return True   # legacy tokens without jti remain valid (backward compatible)
    s = db.query(models.OperatorSession).filter_by(jti=jti).first()
    if s is None:
        return True   # token predates session tracking → allowed
    return _effective_status(s) == "active"


def touch(db, jti, commit=True):
    s = db.query(models.OperatorSession).filter_by(jti=jti).first() if jti else None
    if s:
        s.last_seen_at = datetime.datetime.utcnow()
        if commit:
            db.commit()
    return s


def revoke(db, session_id, commit=True):
    s = db.get(models.OperatorSession, session_id)
    if s and s.status == "active":
        s.status = "revoked"
        s.revoked_at = datetime.datetime.utcnow()
        if commit:
            db.commit()
    return s


def revoke_by_jti(db, jti, commit=True):
    s = db.query(models.OperatorSession).filter_by(jti=jti).first() if jti else None
    if s:
        return revoke(db, s.id, commit=commit)
    return None


def set_break_glass(db, jti, on, commit=True):
    s = db.query(models.OperatorSession).filter_by(jti=jti).first() if jti else None
    if s:
        s.break_glass = bool(on)
        if commit:
            db.commit()
    return s


def presence(db, minutes=15):
    """Operators seen active within the window (for 'who is online' / conflict awareness)."""
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(minutes=minutes)
    rows = (db.query(models.OperatorSession)
            .filter(models.OperatorSession.status == "active").all())
    out = {}
    for s in rows:
        ls = s.last_seen_at
        if ls is not None and ls.tzinfo is not None:
            ls = ls.replace(tzinfo=None)
        if ls is None or ls >= cutoff:
            out[s.operator_id] = {"operator_id": s.operator_id,
                                  "break_glass": bool(s.break_glass),
                                  "last_seen": (s.last_seen_at.isoformat() if s.last_seen_at else None)}
    return list(out.values())


def to_dict(s) -> dict:
    return {"id": s.id, "operator_id": s.operator_id, "device": s.device, "ip": s.ip,
            "mfa_state": s.mfa_state, "break_glass": bool(s.break_glass),
            "status": _effective_status(s),
            "created_at": (s.created_at.isoformat() if s.created_at else None),
            "last_seen_at": (s.last_seen_at.isoformat() if s.last_seen_at else None)}
