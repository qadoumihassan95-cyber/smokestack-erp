"""Approval workflow — generic M-of-N / sequential approvals with separation of duties.

Used by break-glass elevation and (future) god-tier/bulk commands. Invariants:
  * A mandatory reason is required to open a request.
  * The requester can NEVER approve their own request (SoD).
  * An approver votes at most once.
  * A request approves only when a quorum of DISTINCT approvers approve; any reject fails it.
  * Requests expire; expired requests cannot be decided.
Every transition is auditable (the caller records audit/events).
"""
from __future__ import annotations

import datetime

import models


class ApprovalError(Exception):
    def __init__(self, code, http=409):
        super().__init__(code)
        self.code = code
        self.http = http


def create_request(db, requester_id, *, subject_type, subject_ref=None, policy="single",
                   quorum_required=1, reason="", correlation_id=None, ttl_seconds=3600,
                   commit=True) -> "models.ApprovalRequest":
    if not reason or not reason.strip():
        raise ApprovalError("reason_required", 422)
    if policy not in ("single", "m_of_n", "sequential"):
        raise ApprovalError("invalid_policy", 422)
    q = max(1, quorum_required if policy != "single" else 1)
    req = models.ApprovalRequest(
        subject_type=subject_type, subject_ref=(str(subject_ref) if subject_ref is not None else None),
        requested_by=requester_id, policy=policy, quorum_required=q, status="pending",
        reason=reason, correlation_id=correlation_id,
        expires_at=datetime.datetime.utcnow() + datetime.timedelta(seconds=ttl_seconds))
    db.add(req)
    if commit:
        db.commit()
    else:
        db.flush()
    return req


def _is_expired(req) -> bool:
    exp = req.expires_at
    if exp is None:
        return False
    now = datetime.datetime.now(exp.tzinfo) if exp.tzinfo else datetime.datetime.utcnow()
    return now >= exp


def decide(db, approver_id, request_id, decision, reason="", commit=True) -> "models.ApprovalRequest":
    req = db.get(models.ApprovalRequest, request_id)
    if not req:
        raise ApprovalError("request_not_found", 404)
    if req.status != "pending":
        raise ApprovalError(f"already_{req.status}", 409)
    if _is_expired(req):
        req.status = "expired"
        db.commit()
        raise ApprovalError("request_expired", 409)
    if decision not in ("approve", "reject"):
        raise ApprovalError("invalid_decision", 422)
    if approver_id == req.requested_by:
        raise ApprovalError("self_approval_forbidden", 403)
    if db.query(models.Approval).filter_by(request_id=request_id, approver_id=approver_id).first():
        raise ApprovalError("already_voted", 409)

    db.add(models.Approval(request_id=request_id, approver_id=approver_id,
                           decision=decision, reason=reason,
                           sequence=db.query(models.Approval).filter_by(request_id=request_id).count()))
    db.flush()   # ensure the just-cast vote is counted below (session autoflush is off)
    if decision == "reject":
        req.status = "rejected"
        req.decided_at = datetime.datetime.utcnow()
    else:
        approvals = (db.query(models.Approval)
                     .filter_by(request_id=request_id, decision="approve").count())
        if approvals >= req.quorum_required:
            req.status = "approved"
            req.decided_at = datetime.datetime.utcnow()
    if commit:
        db.commit()
    else:
        db.flush()
    return req


def cancel(db, requester_id, request_id, commit=True) -> "models.ApprovalRequest":
    req = db.get(models.ApprovalRequest, request_id)
    if not req:
        raise ApprovalError("request_not_found", 404)
    if req.requested_by != requester_id:
        raise ApprovalError("only_requester_may_cancel", 403)
    if req.status != "pending":
        raise ApprovalError(f"already_{req.status}", 409)
    req.status = "cancelled"
    req.decided_at = datetime.datetime.utcnow()
    if commit:
        db.commit()
    return req


def expire_due(db) -> int:
    n = 0
    for req in db.query(models.ApprovalRequest).filter_by(status="pending").all():
        if _is_expired(req):
            req.status = "expired"
            req.decided_at = datetime.datetime.utcnow()
            n += 1
    if n:
        db.commit()
    return n


def to_dict(req) -> dict:
    return {"id": req.id, "subject_type": req.subject_type, "subject_ref": req.subject_ref,
            "requested_by": req.requested_by, "policy": req.policy,
            "quorum_required": req.quorum_required, "status": req.status, "reason": req.reason,
            "correlation_id": req.correlation_id,
            "expires_at": (req.expires_at.isoformat() if req.expires_at else None)}
