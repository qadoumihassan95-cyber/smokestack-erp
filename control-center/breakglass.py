"""Break-glass — just-in-time, time-boxed privilege elevation for god-tier operations.

Never a standing privilege. Two paths:
  * Normal: an M-of-N ApprovalRequest gates the grant; it activates only on quorum and expires
    after a short TTL. The Platform Owner has NO unilateral god-tier path (SoD via approvals).
  * Offline recovery (PDP-independent): unlocked by a separately-held emergency credential for
    when the live authorization plane is down. Maximally recorded, high-severity, post-reviewed.

Recording: an elevated session must be recorded; if recording fails, the grant is revoked
immediately (no unrecorded elevated access). Recording storage lives in-region (metadata handle
here; the store is a later phase).
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import os

import approvals
import models

# Emergency offline credential secret — deliberately DISTINCT from the JWT secret and the PDP.
_OFFLINE_SECRET = os.environ.get("OFFLINE_BREAK_GLASS_SECRET", "")


class BreakGlassError(Exception):
    def __init__(self, code, http=409):
        super().__init__(code)
        self.code = code
        self.http = http


def _now():
    return datetime.datetime.utcnow()


def request(db, operator_id, capability, reason, *, quorum=2, correlation_id=None,
            commit=True) -> "models.ElevationGrant":
    """Open a break-glass elevation request (pending until quorum approves). Mandatory reason."""
    if not reason or not reason.strip():
        raise BreakGlassError("reason_required", 422)
    req = approvals.create_request(
        db, operator_id, subject_type="elevation", policy="m_of_n", quorum_required=quorum,
        reason=reason, correlation_id=correlation_id, ttl_seconds=1800, commit=False)
    db.flush()
    grant = models.ElevationGrant(
        operator_id=operator_id, capability=capability, reason=reason, status="pending",
        approval_request_id=req.id, offline=False, correlation_id=correlation_id)
    db.add(grant)
    if commit:
        db.commit()
    else:
        db.flush()
    return grant


def approve(db, approver_id, grant_id, decision="approve", reason="", *,
            elevated_ttl_seconds=900, commit=True) -> "models.ElevationGrant":
    """Record an approver's decision; activate the (time-boxed) grant on quorum."""
    grant = db.get(models.ElevationGrant, grant_id)
    if not grant:
        raise BreakGlassError("grant_not_found", 404)
    if grant.status != "pending":
        raise BreakGlassError(f"already_{grant.status}", 409)
    req = approvals.decide(db, approver_id, grant.approval_request_id, decision, reason, commit=False)
    if req.status == "rejected":
        grant.status = "rejected"
    elif req.status == "approved":
        grant.status = "active"
        grant.activated_at = _now()
        grant.expires_at = _now() + datetime.timedelta(seconds=elevated_ttl_seconds)
    if commit:
        db.commit()
    else:
        db.flush()
    return grant


def open_offline(db, operator_id, capability, reason, credential, *,
                 elevated_ttl_seconds=900, commit=True) -> "models.ElevationGrant":
    """PDP-independent recovery path. Validates a separately-held offline credential (HMAC over
    operator+capability). Does not consult the live approval plane — for use when it is down."""
    if not reason or not reason.strip():
        raise BreakGlassError("reason_required", 422)
    if not _OFFLINE_SECRET:
        raise BreakGlassError("offline_path_not_configured", 503)
    expected = hmac.new(_OFFLINE_SECRET.encode(), f"{operator_id}:{capability}".encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, credential or ""):
        raise BreakGlassError("invalid_offline_credential", 403)
    grant = models.ElevationGrant(
        operator_id=operator_id, capability=capability, reason=reason, status="active",
        offline=True, activated_at=_now(),
        expires_at=_now() + datetime.timedelta(seconds=elevated_ttl_seconds))
    db.add(grant)
    if commit:
        db.commit()
    else:
        db.flush()
    return grant


def start_recording(db, grant_id, ref, commit=True) -> "models.ElevationGrant":
    grant = db.get(models.ElevationGrant, grant_id)
    if grant:
        grant.recording_ref = ref
        if commit:
            db.commit()
    return grant


def on_recording_failure(db, grant_id, commit=True) -> "models.ElevationGrant":
    """Recording failure terminates elevated access — no unrecorded god-mode."""
    return revoke(db, grant_id, "recording_failed", commit=commit)


def revoke(db, grant_id, reason="revoked", commit=True) -> "models.ElevationGrant":
    grant = db.get(models.ElevationGrant, grant_id)
    if grant and grant.status in ("active", "pending"):
        grant.status = "revoked"
        grant.revoked_at = _now()
        grant.revoked_reason = reason
        if commit:
            db.commit()
    return grant


def active_grant(db, operator_id, capability, grant_id=None):
    """Return a live, unexpired, matching grant for this operator+capability, or None."""
    q = db.query(models.ElevationGrant).filter_by(operator_id=operator_id, status="active")
    if grant_id is not None:
        q = q.filter(models.ElevationGrant.id == grant_id)
    for g in q.order_by(models.ElevationGrant.id.desc()).all():
        exp = g.expires_at
        expired = False
        if exp is not None:
            now = datetime.datetime.now(exp.tzinfo) if exp.tzinfo else _now()
            expired = now >= exp
        if expired:
            g.status = "expired"
            db.commit()
            continue
        if g.capability in ("*", capability):
            return g
    return None


def check(db, operator_id, capability, grant_id=None) -> tuple[bool, str]:
    if active_grant(db, operator_id, capability, grant_id) is not None:
        return True, "elevation_active"
    return False, "no_active_elevation"


def to_dict(g) -> dict:
    return {"id": g.id, "operator_id": g.operator_id, "capability": g.capability,
            "status": g.status, "offline": bool(g.offline), "reason": g.reason,
            "approval_request_id": g.approval_request_id, "recording_ref": g.recording_ref,
            "expires_at": (g.expires_at.isoformat() if g.expires_at else None)}
