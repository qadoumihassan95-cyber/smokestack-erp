# Phase Report — M1: Mission Control Foundation (first vertical slice)

**Phase name:** M1 — Foundation triad (Operator IAM skeleton + Typed Command Pipeline + hash-chained Audit), proven on one real mutation.
**Branch:** `feature/platform-owner-mission-control` (work staged in the control-center working copy; push to GitHub is a pending publish step — see Deployment).
**Objective:** establish the single audited, authorized, idempotent, concurrency-safe path that every future mutating Center must use, and prove it end-to-end on the feature-flag enable/disable mutation — additive, flag-guarded, zero regressions.

## Existing functionality reused (no rewrite)
- Feature-flag engine, `_flag_snapshot` / `_log_flag_audit`, operator auth (`current_operator`, `require_owner`, `require_internal`), `platform_audit_log`, additive-migration + inspector-guard pattern (from `0002`/`0003`), the 77-test harness.

## Architecture implemented
- **Operator IAM PDP** (`iam.py`): 10 least-privilege roles, role→capability map, ABAC scope check, `decide(op, action, target, ctx)` **deny-by-default**; backward-compatible mapping of the legacy coarse `platform_role` (owner→platform_owner, etc.) and full-scope fallback for the seeded owner so the live console is unaffected. SoD registry (`requires_sod`).
- **Typed Command Pipeline** (`commands.py`): `Command` envelope + `dispatch()` implementing the mandated lifecycle — authenticate → authorize (PDP) → SoD/approval → execute (with **executor-side authoritative-state revalidation**) → result-validate → hash-chained audit. **Idempotency** (unique key → exactly-once replay), **optimistic concurrency** (`expected_version`), deterministic **blast-radius** classifier, fail-closed at every step. Executors registered per command type.
- **Hash-chained audit** (`audit_chain.py`): `append()` chains `entry_hash = sha256(prev_hash + canonical(row))` over `platform_audit_log`; `verify()` re-walks and detects tampering; links across interleaved legacy rows; legacy rows are pre-chain genesis.
- **Proven on:** `feature_flag.set_state` executor (re-reads the flag, enforces `expected_version`, toggles `default_state`, bumps `version`, writes feature-flag audit) via `POST /api/platform/commands`.

## Files created
`iam.py`, `commands.py`, `audit_chain.py`, `migrations/versions/0004_mission_control_foundation.py`, `tests/test_mission_control.py`, `docs/MISSION_CONTROL_IMPLEMENTATION_ASSESSMENT.md`, `docs/PHASE_M1_REPORT.md`.

## Files modified
`models.py` (additive: `operators.roles/scopes`, `feature_flags.version`, `platform_audit_log.{correlation_id,command_type,idempotency_key,prev_hash,entry_hash}`, new `CommandLog`), `main.py` (imports; `feature_flag.set_state` executor; `POST /api/platform/commands`; `GET /api/platform/commands`; `GET /api/platform/audit/verify`; version bump in existing flag PATCH). **No behavior removed; every prior endpoint unchanged.**

## Database migrations
`0004_mc_foundation` (down_revision `0003_dev_platform`): additive, inspector-guarded, reversible. **Verified `upgrade head → downgrade 0003 → upgrade head`** clean on SQLite (command_log/roles/entry_hash/version present→absent→present). Additive-only ⇒ live SmokeStack / Company #1 unaffected.

## APIs added
`POST /api/platform/commands` (governed command execution), `GET /api/platform/commands` (command log), `GET /api/platform/audit/verify` (owner: chain integrity).

## Events / Permissions / Policies
- **Events (audit):** `command.completed|failed|rejected` hash-chained with correlation + idempotency key.
- **Permissions:** new capability `feature_flag.set_state` gated by the PDP; deny-by-default for roles without it (e.g. `read_only_auditor`); ABAC scope by ERP/region/env/customer.
- **Policies:** SoD registry (`SOD_REQUIRED`) — irreversible/god-tier actions cannot be self-approved.

## UI screens added
None this slice (API-first, per ADR W1/W8). Console wiring (break-glass banner, command result surfacing, permission-aware nav) is M2.

## Tests added / executed / results
Added `tests/test_mission_control.py` (10 tests): PDP owner-allowed/auditor-denied, PDP out-of-scope denied, command requires auth, governed toggle happy-path (+audit+command-log), **idempotent replay no double-apply**, **stale expected_version rejected (409)**, unknown-type rejected, missing-idempotency-key rejected, **SoD self-approval forbidden + approval-required**, **audit chain verifies then detects tampering**.
**Results:** `10 passed`. Full regression `tests/test_control_center.py` `77 passed`. **Total 87 passing.** `ruff` clean. Migration up/down/up clean.

## Security review
Deny-by-default authorization proven; no self-approval; optimistic concurrency prevents stale-state action; idempotency prevents double-apply; audit tamper-evident and verifiable; console remains an API client (no new privileged backdoor); no secrets introduced; legacy owner retains function via scope fallback (documented, tightened in M2 when real operators are seeded).

## Performance review
All new paths are single-row, synchronous, indexed by PK / unique idempotency key. No fleet operations. Well within the p99 < 500 ms command-ack target at this scope.

## Backward-compatibility status
Fully backward compatible. Additive migration; all prior endpoints and 77 tests unchanged; live business unaffected (Company #1 semantics preserved).

## Development deployment status
Not deployed. Staged on the feature branch working copy, Development-only per the delivery rules. Push to GitHub `feature/platform-owner-mission-control` + Render Development deploy is a **publish action pending your approval**.

## Known limitations
- SoD "valid second approver" happy-path not exercised (seed has one operator); deny paths (self/missing) are covered. To close in M2 when multiple operators seed.
- Approval **workflow** (request/grant records), JIT elevation, break-glass, session recording, offline recovery = M2.
- Only `feature_flag.set_state` is routed through the pipeline; other mutations still use their existing endpoints (intentional — migrated incrementally per phase).
- Audit chain is append-only in code; DB-level WORM/immutability is a later hardening step.

## Risks
- Incremental endpoint migration means two mutation styles coexist transiently; mitigated by keeping old endpoints working and migrating per phase behind flags.
- Legacy full-scope fallback for the seeded owner must be replaced by explicit scoped grants before additional operators exist (tracked for M2).

## Recommended next phase
**M2 — Foundation depth:** JIT elevation + break-glass (M-of-N, approver-unavailable escalation, offline path) + approval-workflow records + privileged-session model; Event Backbone (transactional outbox); first CQRS read model; BFF with delegated identity (token exchange). Then M3 (Bulk-Ops Safety Engine).

## Git commit hash
Pending — commit/push to `feature/platform-owner-mission-control` is the approval-gated publish step; the hash will be recorded on delivery.
