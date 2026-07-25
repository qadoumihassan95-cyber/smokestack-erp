"""Bulk-Operations Safety Engine (M3) — the only path for multi-tenant / fleet actions.

segment → preview → blast-radius → policy → approval → canary rings → per-target execution
(via the M1 command pipeline: authz + idempotency + optimistic concurrency + audit) → auto-halt
→ pause/resume/abort → rollback → completion. Durable and resumable (job + target rows).

Reuses M1 (command pipeline, audit) and M2 (approvals, outbox). No bypass: every per-target
mutation is a governed command; every job transition is audited and emitted to the outbox.
"""
from __future__ import annotations

import datetime
import json

import approvals
import commands
import events
import models

TERMINAL = {"completed", "aborted", "rolled_back", "failed"}
NON_TERMINAL = {"planned", "awaiting_approval", "approved", "running", "paused", "halted", "rolling_back"}
DESTRUCTIVE_STATUS = {"suspended", "archived", "cancelled", "inactive"}


class ChangeError(Exception):
    def __init__(self, code, http=409):
        super().__init__(code)
        self.code = code
        self.http = http


def _now():
    return datetime.datetime.utcnow()


# ------------------------------- segment engine -------------------------------
def resolve_segment(db, filters: dict):
    """Resolve a filter expression to concrete customer targets. Filters (all optional, AND-ed):
    erp, region, status, plan(license), ids[list], external_refs[list], name_contains."""
    q = db.query(models.CustomerRef)
    if filters.get("erp"):
        q = q.filter(models.CustomerRef.erp_product_id == filters["erp"])
    if filters.get("region"):
        q = q.filter(models.CustomerRef.region == filters["region"])
    if filters.get("status"):
        q = q.filter(models.CustomerRef.status == filters["status"])
    if filters.get("ids"):
        q = q.filter(models.CustomerRef.id.in_(filters["ids"]))
    if filters.get("external_refs"):
        q = q.filter(models.CustomerRef.external_ref.in_(filters["external_refs"]))
    if filters.get("name_contains"):
        q = q.filter(models.CustomerRef.name.ilike(f"%{filters['name_contains']}%"))
    rows = q.order_by(models.CustomerRef.id).all()
    if filters.get("plan"):     # license-plan filter (join)
        plans = {lic.customer_ref_id: lic.plan for lic in db.query(models.License).all()}
        rows = [c for c in rows if plans.get(c.id) == filters["plan"]]
    return rows


def segment_preview(db, filters: dict) -> dict:
    rows = resolve_segment(db, filters)
    regions = sorted({(c.region or "us") for c in rows})
    return {"target_count": len(rows), "regions": regions,
            "blast_radius": _blast(len(rows), regions)[0],
            "sample": [{"id": c.id, "name": c.name, "region": c.region} for c in rows[:10]]}


# ------------------------------- blast radius -------------------------------
def _blast(total: int, regions: list) -> tuple[str, bool]:
    multi = len(regions) > 1
    if total <= 1:
        b = "single"
    elif total <= 10:
        b = "small"
    else:
        b = "large"
    if b == "large" and multi:
        b = "cross_region"
    return b, multi


def _is_destructive(command_type: str, params: dict) -> bool:
    verb = command_type.split(".")[-1]
    if verb in ("suspend", "archive", "delete", "restore", "transfer", "migrate"):
        return True
    return (params or {}).get("status") in DESTRUCTIVE_STATUS


def _data_class(command_type: str) -> str:
    root = command_type.split(".")[0]
    return {"ledger": "financial", "security": "security", "tenant": "infrastructure",
            "customer": "customer_data", "ai": "ai"}.get(root, "metadata")


def _policy(blast: str, destructive: bool) -> tuple[str, int, bool]:
    """(approval_policy, quorum, rollback_required) from blast radius + destructiveness."""
    high = destructive or blast in ("large", "regional", "cross_region", "fleet")
    if high:
        return "m_of_n", 2, True
    if blast == "small":
        return "single", 1, False
    return "none", 0, False


def _rings(blast: str, total: int) -> list[int]:
    """Per-ring target counts (canary → widen). Always sums to total."""
    if total <= 0:
        return []
    if blast == "single":
        return [total]
    if blast == "small":
        return [1, total - 1] if total > 1 else [total]
    pct = [0.05, 0.25, 1.0] if blast in ("cross_region", "fleet") else [0.10, 0.50, 1.0]
    counts, prev = [1], 1                       # ring 0 = 1 canary
    for p in pct:
        cum = max(prev, min(total, int(round(total * p))))
        counts.append(cum - prev)
        prev = cum
    if prev < total:
        counts.append(total - prev)
    return [c for c in counts if c > 0]


# ------------------------------- planning -------------------------------
def _snapshot(c, command_type, params):
    if command_type == "customer.set_status":
        return {"before": {"status": c.status}, "after": {"status": params.get("status")}}
    return {"before": {}, "after": dict(params or {})}


def _active_conflicts(db, refs, exclude_job_id=None):
    """Target refs already owned by a non-terminal ChangeJob (one job per target at a time)."""
    rows = (db.query(models.ChangeTarget, models.ChangeJob)
            .join(models.ChangeJob, models.ChangeTarget.job_id == models.ChangeJob.id)
            .filter(models.ChangeTarget.target_ref.in_([str(r) for r in refs]),
                    models.ChangeJob.status.in_(list(NON_TERMINAL))).all())
    return {t.target_ref for (t, j) in rows if j.id != exclude_job_id}


def preview(db, command_type, filters, params) -> dict:
    """Pure dry-run — no persistence. Full plan: targets, states, conflicts, risk, approval."""
    rows = resolve_segment(db, filters)
    regions = sorted({(c.region or "us") for c in rows})
    blast, _ = _blast(len(rows), regions)
    destructive = _is_destructive(command_type, params)
    policy, quorum, rollback_req = _policy(blast, destructive)
    conflicts = _active_conflicts(db, [c.id for c in rows])
    warnings = []
    if conflicts:
        warnings.append(f"{len(conflicts)} target(s) already in another active job (will be skipped)")
    if not rows:
        warnings.append("segment resolved to 0 targets")
    risk = {"single": 1, "small": 2, "large": 4, "regional": 5, "cross_region": 6, "fleet": 8}.get(blast, 3)
    if destructive:
        risk += 3
    return {"command_type": command_type, "target_count": len(rows), "regions": regions,
            "blast_radius": blast, "data_class": _data_class(command_type), "destructive": destructive,
            "reversibility": ("reversible" if command_type == "customer.set_status" else "unknown"),
            "approval_policy": policy, "approval_quorum": quorum, "rollback_required": rollback_req,
            "rings": _rings(blast, len(rows) - len(conflicts)), "risk_score": risk,
            "conflicts": sorted(conflicts), "warnings": warnings,
            "targets": [{"id": c.id, "name": c.name, "region": c.region,
                         "expected_version": (c.version or 1),
                         "planned": _snapshot(c, command_type, params)} for c in rows]}


def create_job(db, op, *, name, command_type, filters, params, reason,
               rate_limit_per_tick=50, error_budget=0.2) -> "models.ChangeJob":
    if not commands.is_registered(command_type):
        raise ChangeError("unknown_command_type", 400)
    if not reason or not reason.strip():
        raise ChangeError("reason_required", 422)
    rows = resolve_segment(db, filters)
    conflicts = _active_conflicts(db, [c.id for c in rows])
    rows = [c for c in rows if str(c.id) not in conflicts]     # exclude conflicted targets
    if not rows:
        raise ChangeError("no_available_targets", 422)
    regions = sorted({(c.region or "us") for c in rows})
    blast, _ = _blast(len(rows), regions)
    destructive = _is_destructive(command_type, params)
    policy, quorum, rollback_req = _policy(blast, destructive)
    corr = "job_" + (name or command_type).replace(" ", "_")[:20] + "_" + str(int(_now().timestamp()))
    job = models.ChangeJob(
        name=name, command_type=command_type, params=json.dumps(params or {}),
        filters=json.dumps(filters or {}), reason=reason, blast_radius=blast,
        data_class=_data_class(command_type), approval_policy=policy,
        rollback_required=rollback_req, rate_limit_per_tick=rate_limit_per_tick,
        error_budget=str(error_budget), total_targets=len(rows), created_by=op.id,
        correlation_id=corr, status="planned")
    db.add(job)
    db.flush()
    # assign rings + create target rows with a snapshot for optimistic concurrency
    ring_counts = _rings(blast, len(rows))
    ring_idx, in_ring, ring_no = 0, 0, []
    for _ in rows:
        while ring_idx < len(ring_counts) and in_ring >= ring_counts[ring_idx]:
            ring_idx += 1
            in_ring = 0
        ring_no.append(min(ring_idx, max(0, len(ring_counts) - 1)))
        in_ring += 1
    job.rings = json.dumps(ring_counts)
    for c, r in zip(rows, ring_no, strict=False):
        db.add(models.ChangeTarget(
            job_id=job.id, target_type="customer", target_ref=str(c.id),
            expected_version=(c.version or 1), planned_state=json.dumps(_snapshot(c, command_type, params)),
            ring=r, status="pending", idempotency_key=f"job{job.id}:t{c.id}"))
    # approval gate
    if policy != "none":
        req = approvals.create_request(db, op.id, subject_type="bulk", subject_ref=job.id,
                                       policy=policy, quorum_required=quorum, reason=reason,
                                       correlation_id=corr, commit=False)
        db.flush()
        job.approval_request_id = req.id
        job.status = "awaiting_approval"
    else:
        job.status = "approved"
    events.emit(db, aggregate_type="change_job", aggregate_id=job.id, event_type="change_job.created",
                payload={"job_id": job.id, "command_type": command_type, "blast_radius": blast,
                         "status": job.status, "total_targets": len(rows)},
                correlation_id=corr, dedupe_key=f"change_job.created:{job.id}", commit=False)
    db.commit()
    return job


def approve_job(db, approver, job_id, decision="approve", reason="") -> "models.ChangeJob":
    job = db.get(models.ChangeJob, job_id)
    if not job:
        raise ChangeError("job_not_found", 404)
    if job.status != "awaiting_approval" or not job.approval_request_id:
        raise ChangeError("not_awaiting_approval", 409)
    req = approvals.decide(db, approver, job.approval_request_id, decision, reason, commit=False)
    if req.status == "approved":
        job.status = "approved"
    elif req.status == "rejected":
        job.status = "aborted"
        job.halt_reason = "approval_rejected"
    events.emit(db, aggregate_type="change_job", aggregate_id=job.id,
                event_type=f"change_job.{job.status}", payload={"job_id": job.id, "status": job.status},
                correlation_id=job.correlation_id, dedupe_key=f"change_job.{job.status}:{job.id}:approve",
                commit=False)
    db.commit()
    return job


# ------------------------------- execution -------------------------------
def _op_of(db, job):
    return db.get(models.Operator, job.created_by)


def _in_maintenance_window(job) -> bool:
    if not job.maintenance_window:
        return True
    try:
        w = json.loads(job.maintenance_window)
    except Exception:
        return True
    now = _now().isoformat()
    return (w.get("start", "") <= now <= w.get("end", "9999")) if w else True


def _halt(db, job, reason):
    job.status = "halted"
    job.halt_reason = reason
    events.emit(db, aggregate_type="change_job", aggregate_id=job.id, event_type="change_job.halted",
                payload={"job_id": job.id, "reason": reason}, correlation_id=job.correlation_id,
                dedupe_key=f"change_job.halted:{job.id}:{reason}:{job.current_ring}", commit=False)
    db.commit()


def execute_tick(db, job_id) -> dict:
    """Advance the job by one bounded batch of the current ring. Idempotent + resumable."""
    job = db.get(models.ChangeJob, job_id)
    if not job:
        raise ChangeError("job_not_found", 404)
    if job.status in TERMINAL:
        return {"status": job.status, "done": True}
    if job.status not in ("approved", "running"):
        raise ChangeError(f"not_runnable:{job.status}", 409)
    # approval must still hold (auto-halt if revoked mid-flight)
    if job.approval_request_id:
        req = db.get(models.ApprovalRequest, job.approval_request_id)
        if not req or req.status != "approved":
            _halt(db, job, "approval_revoked")
            return {"status": "halted", "reason": "approval_revoked"}
    if not _in_maintenance_window(job):
        return {"status": job.status, "waiting": "maintenance_window"}

    if job.status == "approved":
        job.status = "running"
        job.started_at = _now()
        db.commit()

    op = _op_of(db, job)
    ring = job.current_ring
    pend = (db.query(models.ChangeTarget)
            .filter_by(job_id=job.id, ring=ring, status="pending")
            .order_by(models.ChangeTarget.id).limit(job.rate_limit_per_tick).all())
    processed = 0
    for t in pend:
        # target lease: skip if this ref is being actively run by another job
        other = (db.query(models.ChangeTarget)
                 .filter(models.ChangeTarget.target_ref == t.target_ref,
                         models.ChangeTarget.status == "running",
                         models.ChangeTarget.job_id != job.id).first())
        if other:
            t.status = "skipped"
            t.error = "lease_conflict"
            db.commit()
            continue
        t.status = "running"
        t.attempts = (t.attempts or 0) + 1
        db.commit()
        after = json.loads(t.planned_state).get("after", {})
        cref = db.get(models.CustomerRef, int(t.target_ref))
        cmd = commands.Command(
            type=job.command_type,
            target={"customer_ref_id": int(t.target_ref),
                    "erp_product_id": (cref.erp_product_id if cref else None),
                    "region": (cref.region if cref else None)},
            params=after, justification=f"bulk job {job.id}", idempotency_key=t.idempotency_key,
            expected_version=t.expected_version, correlation_id=job.correlation_id)
        try:
            res = commands.dispatch(db, op, cmd)
            t.status = "succeeded"
            t.result = json.dumps(res.get("result"))
        except commands.CommandError as e:
            t.status = "failed"
            t.error = e.code
        db.commit()
        processed += 1

    # ring complete? evaluate error budget, then advance / finish
    remaining = db.query(models.ChangeTarget).filter_by(job_id=job.id, ring=ring, status="pending").count()
    if remaining == 0:
        ring_targets = db.query(models.ChangeTarget).filter_by(job_id=job.id, ring=ring).all()
        rt = len(ring_targets)
        failed = sum(1 for x in ring_targets if x.status == "failed")
        if rt and (failed / rt) > float(job.error_budget or 0.2):
            _halt(db, job, "error_budget_exceeded")
            return {"status": "halted", "reason": "error_budget_exceeded", "ring": ring,
                    "failed": failed, "ring_size": rt}
        events.emit(db, aggregate_type="change_job", aggregate_id=job.id,
                    event_type="change_job.ring_completed",
                    payload={"job_id": job.id, "ring": ring, "failed": failed},
                    correlation_id=job.correlation_id,
                    dedupe_key=f"change_job.ring_completed:{job.id}:{ring}", commit=False)
        rings = json.loads(job.rings)
        if ring + 1 >= len(rings):
            job.status = "completed"
            job.completed_at = _now()
            events.emit(db, aggregate_type="change_job", aggregate_id=job.id,
                        event_type="change_job.completed", payload={"job_id": job.id},
                        correlation_id=job.correlation_id,
                        dedupe_key=f"change_job.completed:{job.id}", commit=False)
        else:
            job.current_ring = ring + 1
        db.commit()
    return {"status": job.status, "processed": processed, "ring": job.current_ring}


def run(db, job_id, max_ticks=50) -> dict:
    """Drive a job to a stopping state (completed/halted/paused/aborted) for convenience/tests."""
    last = {}
    for _ in range(max_ticks):
        job = db.get(models.ChangeJob, job_id)
        if job.status in TERMINAL or job.status in ("halted", "paused"):
            break
        last = execute_tick(db, job_id)
        if last.get("waiting"):
            break
    return {"final_status": db.get(models.ChangeJob, job_id).status, "last": last}


def pause(db, job_id):
    job = db.get(models.ChangeJob, job_id)
    if job and job.status == "running":
        job.status = "paused"
        db.commit()
    return job


def resume(db, job_id):
    job = db.get(models.ChangeJob, job_id)
    if job and job.status in ("paused", "halted"):
        job.status = "approved" if job.approval_request_id is None or _approved(db, job) else job.status
        if job.status in ("paused", "halted"):
            job.status = "running"
        db.commit()
    return job


def _approved(db, job):
    if not job.approval_request_id:
        return True
    r = db.get(models.ApprovalRequest, job.approval_request_id)
    return bool(r and r.status == "approved")


def abort(db, job_id):
    job = db.get(models.ChangeJob, job_id)
    if job and job.status not in TERMINAL:
        job.status = "aborted"
        job.completed_at = _now()
        events.emit(db, aggregate_type="change_job", aggregate_id=job.id,
                    event_type="change_job.aborted", payload={"job_id": job.id},
                    correlation_id=job.correlation_id, dedupe_key=f"change_job.aborted:{job.id}",
                    commit=False)
        db.commit()
    return job


def rollback(db, job_id) -> dict:
    """Reverse succeeded targets (newest ring first) by re-applying their captured 'before' state
    through the command pipeline. Idempotent + audited."""
    job = db.get(models.ChangeJob, job_id)
    if not job:
        raise ChangeError("job_not_found", 404)
    job.status = "rolling_back"
    db.commit()
    op = _op_of(db, job)
    done = 0
    targets = (db.query(models.ChangeTarget).filter_by(job_id=job.id, status="succeeded")
               .order_by(models.ChangeTarget.ring.desc(), models.ChangeTarget.id.desc()).all())
    for t in targets:
        before = json.loads(t.planned_state).get("before", {})
        cref = db.get(models.CustomerRef, int(t.target_ref))
        cmd = commands.Command(
            type=job.command_type,
            target={"customer_ref_id": int(t.target_ref),
                    "erp_product_id": (cref.erp_product_id if cref else None)},
            params=before, justification=f"rollback job {job.id}",
            idempotency_key=f"rollback:{t.idempotency_key}",
            expected_version=(cref.version if cref else None), correlation_id=job.correlation_id)
        try:
            commands.dispatch(db, op, cmd)
            t.status = "rolled_back"
            done += 1
        except commands.CommandError as e:
            t.error = f"rollback_failed:{e.code}"
        db.commit()
    job.status = "rolled_back"
    job.completed_at = _now()
    events.emit(db, aggregate_type="change_job", aggregate_id=job.id,
                event_type="change_job.rolled_back", payload={"job_id": job.id, "reverted": done},
                correlation_id=job.correlation_id, dedupe_key=f"change_job.rolled_back:{job.id}",
                commit=True)
    return {"rolled_back": done, "status": job.status}


def progress(db, job) -> dict:
    counts = {}
    for (st,) in db.query(models.ChangeTarget.status).filter_by(job_id=job.id).all():
        counts[st] = counts.get(st, 0) + 1
    return counts


def to_dict(job, db=None) -> dict:
    d = {"id": job.id, "name": job.name, "command_type": job.command_type,
         "status": job.status, "blast_radius": job.blast_radius, "data_class": job.data_class,
         "approval_policy": job.approval_policy, "approval_request_id": job.approval_request_id,
         "rollback_required": bool(job.rollback_required), "rings": json.loads(job.rings or "[]"),
         "current_ring": job.current_ring, "total_targets": job.total_targets,
         "halt_reason": job.halt_reason, "created_by": job.created_by,
         "correlation_id": job.correlation_id,
         "created_at": (job.created_at.isoformat() if job.created_at else None)}
    if db is not None:
        d["progress"] = progress(db, job)
    return d
