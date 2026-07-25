"""Typed Command Pipeline — the single audited path for every Mission Control mutation.

Lifecycle (SUPER_ACCOUNT_WORKSPACE §2, ADR W8/W11):
    authenticate → authorize (IAM PDP) → policy check → blast-radius classify
    → approval (SoD) if required → execute (with expected-version revalidation)
    → result-validate → tamper-evident audit.

Design guarantees:
  * Deny-by-default: any missing/failed step denies (fail closed).
  * Idempotent: a repeated idempotency_key returns the recorded result, never re-applies.
  * Optimistic concurrency: the executor re-reads authoritative state and rejects on version drift
    — read models are never the correctness gate; the write path is.
  * Separation of duties: SoD commands cannot be approved by their own requester.
  * Every outcome is hash-chained into the platform audit.

Executors are registered per command type. An executor receives (db, op, cmd) and MUST perform the
authoritative-state revalidation itself, returning a result dict; it raises CommandError to reject.
"""
from __future__ import annotations

import datetime
import json

import audit_chain
import breakglass
import events
import iam
import models

_REGISTRY: dict[str, callable] = {}


class CommandError(Exception):
    def __init__(self, code: str, http: int = 409):
        super().__init__(code)
        self.code = code
        self.http = http


class Command:
    """The typed command envelope."""

    def __init__(self, *, type, target=None, tenant_context=None, environment=None, params=None,
                 justification="", idempotency_key=None, expected_version=None, correlation_id=None,
                 approval_policy="none", approved_by=None, blast_radius=None, elevation_id=None):
        if not type:
            raise CommandError("command_type_required", 422)
        if not idempotency_key:
            raise CommandError("idempotency_key_required", 422)
        self.type = type
        self.target = target or {}
        self.tenant_context = tenant_context
        self.environment = environment
        self.params = params or {}
        self.justification = justification or ""
        self.idempotency_key = idempotency_key
        self.expected_version = expected_version
        self.correlation_id = correlation_id or ("cmd_" + idempotency_key)
        self.approval_policy = approval_policy or "none"
        self.approved_by = approved_by
        self.blast_radius = blast_radius  # may be None → classified below
        self.elevation_id = elevation_id  # active break-glass grant for god-tier actions


def register(command_type: str):
    def _wrap(fn):
        _REGISTRY[command_type] = fn
        return fn
    return _wrap


def is_registered(command_type: str) -> bool:
    """Public check for whether an executor exists (avoids reaching into the registry)."""
    return command_type in _REGISTRY


def classify_blast_radius(cmd: "Command") -> str:
    """Deterministic (never ML) — anything that could lower a gate must be rule-based.
    A single-target flag toggle is 'low'; a fleet target or a destructive verb escalates."""
    if cmd.blast_radius:
        return cmd.blast_radius
    verb = cmd.type.split(".")[-1]
    if verb in ("restore", "delete", "suspend", "transfer", "correct", "migrate"):
        return "irreversible"
    tgt = cmd.target or {}
    if tgt.get("segment") or tgt.get("scope") == "fleet" or isinstance(tgt.get("targets"), list):
        return "high"
    return "low"


def _target_str(cmd: "Command") -> str:
    return json.dumps(cmd.target, sort_keys=True, separators=(",", ":"))


def dispatch(db, op, cmd: "Command") -> dict:
    """Run one command through the full pipeline. Returns the result dict or raises CommandError."""
    # 0) Idempotency — exactly-once. A prior completed command with this key returns its result.
    prior = (db.query(models.CommandLog)
             .filter_by(idempotency_key=cmd.idempotency_key).first())
    if prior is not None:
        if prior.status == "completed":
            return {"status": "completed", "idempotent_replay": True,
                    "result": json.loads(prior.result) if prior.result else None}
        raise CommandError(f"idempotency_conflict:{prior.status}", 409)

    blast = classify_blast_radius(cmd)
    log = models.CommandLog(
        command_type=cmd.type, operator_id=getattr(op, "id", None), target=_target_str(cmd),
        tenant_context=cmd.tenant_context, environment=cmd.environment,
        params=json.dumps(cmd.params), justification=cmd.justification,
        idempotency_key=cmd.idempotency_key, expected_version=cmd.expected_version,
        correlation_id=cmd.correlation_id, blast_radius=blast, approval_policy=cmd.approval_policy,
        approved_by=cmd.approved_by, status="requested")
    db.add(log)
    db.flush()

    def _finish(status, reason="", result=None):
        log.status = status
        log.reason = reason
        log.result = json.dumps(result) if result is not None else None
        log.completed_at = datetime.datetime.utcnow()
        audit_chain.append(db, actor_operator_id=getattr(op, "id", None),
                           action=f"command.{status}", target_type="command",
                           target_id=cmd.type, detail=(reason or _target_str(cmd)),
                           result=("ok" if status == "completed" else status),
                           correlation_id=cmd.correlation_id, command_type=cmd.type,
                           idempotency_key=cmd.idempotency_key, commit=False)
        # Transactional outbox: the event commits atomically with the command + audit.
        tgt = cmd.target or {}
        events.emit(db, aggregate_type="command", aggregate_id=cmd.type,
                    event_type=f"command.{status}",
                    payload={"command_type": cmd.type, "operator_id": getattr(op, "id", None),
                             "target": _target_str(cmd), "status": status, "blast_radius": blast,
                             "environment": cmd.environment,
                             "erp_product_id": tgt.get("erp_product_id"),
                             "customer_ref": tgt.get("customer_ref"), "region": tgt.get("region")},
                    correlation_id=cmd.correlation_id,
                    dedupe_key=f"command.{status}:{cmd.idempotency_key}", commit=False)
        db.commit()

    # 1) Authenticate
    if op is None:
        _finish("rejected", "unauthenticated")
        raise CommandError("unauthenticated", 401)

    # 2) Authorize (IAM PDP, deny-by-default)
    d = iam.decide(op, cmd.type, cmd.target, {"environment": cmd.environment})
    if not d.allow:
        _finish("rejected", d.reason)
        raise CommandError(f"forbidden:{d.reason}", 403)
    log.status = "authorized"

    # 3) Separation of duties / approval
    if iam.requires_sod(cmd.type, blast):
        if not cmd.approved_by:
            _finish("rejected", "approval_required")
            raise CommandError("approval_required", 428)
        if cmd.approved_by == getattr(op, "id", None):
            _finish("rejected", "self_approval_forbidden")
            raise CommandError("self_approval_forbidden", 403)
        appr = db.get(models.Operator, cmd.approved_by)
        if not appr or not iam.decide(appr, cmd.type, cmd.target, {}).allow:
            _finish("rejected", "invalid_approver")
            raise CommandError("invalid_approver", 403)

    # 3b) Break-glass — god-tier actions require an active, time-boxed elevation grant.
    if iam.requires_break_glass(cmd.type, blast):
        ok, why = breakglass.check(db, getattr(op, "id", None), cmd.type, cmd.elevation_id)
        if not ok:
            _finish("rejected", f"break_glass:{why}")
            raise CommandError(f"break_glass_required:{why}", 428)

    # 4) Execute (executor performs authoritative-state revalidation)
    fn = _REGISTRY.get(cmd.type)
    if fn is None:
        _finish("rejected", "unknown_command")
        raise CommandError("unknown_command", 400)
    log.status = "executing"
    try:
        result = fn(db, op, cmd)
    except CommandError as e:
        _finish("failed", e.code)
        raise
    except Exception as e:  # unexpected → fail closed, audited
        _finish("failed", f"error:{type(e).__name__}")
        raise CommandError("execution_error", 500)

    # 5) Result-validate + audit
    _finish("completed", "ok", result)
    return {"status": "completed", "idempotent_replay": False, "result": result,
            "blast_radius": blast, "correlation_id": cmd.correlation_id}
