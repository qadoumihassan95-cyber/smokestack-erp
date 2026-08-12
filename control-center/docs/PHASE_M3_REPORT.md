# Phase Report — M3: Bulk-Operations Safety Engine

**Phase:** M3 — the first enterprise fleet-management capability. **Branch:** `feature/platform-owner-mission-control` (Development only; not pushed — M1+M2+M3 will go up together as one PR).
**Objective:** make every fleet-wide/multi-tenant action a governed, previewed, staged, abortable, reversible orchestration — never a synchronous bulk button. Built entirely on M1 (command pipeline, IAM, hash-chained audit) and M2 (break-glass, approvals, sessions, outbox, CQRS, BFF, streaming); nothing bypassed.

## Architecture implemented
The only path for multi-tenant actions: **segment → preview → blast-radius → policy → approval → canary rings → per-target execution → auto-halt → pause/resume/abort → rollback → audit → completion.** A `ChangeJob` + `ChangeTarget` rows make it **durable and resumable**; every per-target mutation is a governed command through the **M1 pipeline** (authz + per-target idempotency + optimistic concurrency + audit); every job transition is **audited and emitted to the M2 outbox** (consumed by CQRS + streaming + Mission Control).

- **Segment engine** (`changes.resolve_segment` / `segment_preview`): saved + dynamic filters (erp, region, status, plan, ids, external_refs, name) → target set, count, regions, blast estimate. Never "everything" by accident.
- **Change planner** (`preview`, dry-run, no persist): per-target planned before/after, expected_version, conflicts, dependencies, reversibility, warnings, **risk score**, approval requirements.
- **Blast-radius engine** (`_blast`, `_data_class`, `_policy`): single / small / large / cross_region + data-class; classification drives **approval policy, canary rings, rollback requirement**. Destructive status changes escalate to M-of-N.
- **Execution engine** (`execute_tick` / `run`): canary rings, per-tick rate limiting, maintenance windows, **per-target idempotency key** (exactly-once resume), **optimistic concurrency** (expected_version re-validated by the executor), **target leases / conflict detection** (one job per target), pause / resume / abort, per-target failure classification, partial success.
- **Auto-halt**: error-budget-per-ring exceeded, **approval revoked mid-flight**, (extensible: critical alert / infra) → job halts, emits `change_job.halted`.
- **Rollback** (`rollback`): reverses succeeded targets newest-ring-first by re-applying the captured `before` state through the pipeline — **per-target, idempotent, audited**.
- **Change history + Mission Control**: `change_jobs-overview` buckets running / paused / halted / aborted / rolled_back / awaiting_approval / critical; per-job + per-target inspection.

## Files created
`changes.py`, `migrations/versions/0006_bulk_operations.py`, `tests/test_mission_control_m3.py`, `docs/PHASE_M3_REPORT.md`.

## Files modified
`models.py` (additive: `Segment`, `ChangeJob`, `ChangeTarget`; `customer_refs.region` + `customer_refs.version`); `main.py` (imports; `customer.set_status` per-target executor; ~16 M3 endpoints).

## Database migrations
`0006_bulk_ops` (down_revision `0005_mc_m2`): additive, inspector-guarded, **reversible** (verified up→down→up). Only additive columns on `customer_refs`; live SmokeStack / Company #1 unaffected.

## APIs added (~16, REST — CLI/automation-ready)
Segments: `POST /segments`, `GET /segments`, `POST /segments/resolve`. Change jobs: `POST /change-jobs/preview` (dry-run), `POST /change-jobs`, `GET /change-jobs`, `GET /change-jobs/{id}`, `GET /change-jobs/{id}/targets`, `POST /{id}/approve`, `/execute` (tick), `/run`, `/pause`, `/resume`, `/abort`, `/rollback`, and `GET /change-jobs-overview` (Mission Control).

## Events
`change_job.created | approved | ring_completed | halted | completed | aborted | rolled_back` — all via the M2 transactional outbox (feeding the CQRS feed + streaming gateway).

## Tests added / executed / results
`tests/test_mission_control_m3.py` — 14 tests: preview is dry-run (no persist), blast/approval scaling, single-target no-approval run, small-group single approval, **large-group M-of-N (two operators)**, **lease conflict excludes overlapping targets**, **duplicate execution idempotent (no double-apply)**, **stale-version fails + auto-halt on error budget**, **approval revoked mid-execution halts**, pause/resume/abort, **rollback reverts all**, outbox events emitted+relayed, overview/targets endpoints, no-available-targets rejected.
**Results:** M3 `14 passed`; M1 `10`; M2 `14`; regression `test_control_center.py` `77`. **Total 129 passing.** `ruff` clean. Migration up/down/up clean.

## Coverage / Performance
Ring-batched execution with per-tick rate limiting bounds work per call; per-target rows are indexed; idempotency keys are unique-indexed. Tick-based execution is durable/resumable and safe under retry. No unbounded fleet operation exists (rings + rate limit). Within targets at this scope; true tens-of-thousands scale needs the scheduled worker + cross-cell saga (below).

## Security review
No bulk mutation exists outside the engine; every per-target change is an authorized, idempotent, audited command; destructive/large/cross-region scope requires **M-of-N approval**; approval revocation mid-flight auto-halts; one job per target (lease); optimistic concurrency prevents acting on stale state; blast radius + reversibility are computed and surfaced before execution; rollback is governed. Deny-by-default and hash-chained audit preserved.

## Backward-compatibility status
Fully compatible. Additive migration; `customer.set_status` is a new executor; all prior endpoints and the 77-test regression unchanged.

## Development deployment status
Not deployed, not pushed. Staged on the feature branch, Development only — per instruction to land M1+M2+M3 as one foundational PR after M3 validation.

## Known limitations
- **Execution is tick-driven** (`/execute`, `/run`) in M3; a **scheduled worker** to advance jobs autonomously (and honor maintenance windows over time) is a small infra follow-up (same as the M2 relay worker).
- **Cross-cell saga**: rings currently execute within one control-plane DB; the per-cell coordinator + per-cell error budgets (for true multi-cell fleets) land when cells exist (post-tenant-catalog).
- **Auto-retry of failed targets** is manual (re-plan) in M3; automatic bounded retry with backoff is a straightforward extension of the existing `attempts` counter.
- Only `customer.set_status` is wired as a per-target bulk executor (the demonstrable, reversible op); more bulk command types (assign license/module/pack, notify, migrate) register against the same engine with no engine change.

## Risks
- Tick-driven progress means a job pauses between ticks until re-invoked; mitigated by durability (resumable) and the overview surfacing in-flight jobs. The scheduled worker closes this.
- Rollback correctness depends on the per-command `before` snapshot; validated for `customer.set_status`; each new bulk command type must define its reverse semantics (documented requirement).

## Readiness for M4
The engine composes cleanly with the next milestones: **Mission Control** (M4) consumes `change_jobs-overview` + the outbox/streaming feed as Tier-2 signals; **Universal Search** indexes jobs/segments; **Customer 360** shows per-customer change history. Recommended M4 start: Mission Control (raw-signal Tier-2 first) + the scheduled relay/execution worker + jti-threading for SSE. Also begin lifting `policy.py`/`tenancy.py` from SmokeStack into shared Control-Plane services.

## Git commit hash
Pending — M1+M2+M3 will be committed/pushed together to `feature/platform-owner-mission-control` (approval-gated publish step).
