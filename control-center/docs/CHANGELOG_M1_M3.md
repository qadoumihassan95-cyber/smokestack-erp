# Changelog — PFS Mission Control Foundation (M1–M3)

Branch: `feature/platform-owner-mission-control` · Development only · Control Plane (`control-center/`).
All changes are **additive and backward compatible**; the live SmokeStack business and Company #1 are
unaffected.

## Features
- **Operator IAM organization** — 10 least-privilege roles, ABAC scopes, deny-by-default PDP; legacy
  coarse role remains functional (mapped).
- **Typed command pipeline** — single governed path for mutations (feature-flag toggle wired; more
  types added per phase).
- **Break-glass** — JIT time-boxed elevation, M-of-N approval, offline recovery path, recording hooks
  (recording failure terminates access).
- **Approval workflow** — generic M-of-N / sequential, separation of duties, expiry.
- **Operator sessions** — login-bound revocable sessions, presence, MFA/break-glass state.
- **Bulk-Operations Safety Engine** — segments, dry-run preview, blast-radius classification, canary
  rings, rate limiting, maintenance windows, auto-halt, pause/resume/abort, per-target + whole-job
  rollback, change history, Mission-Control roll-up.
- **Per-target executor** `customer.set_status` (reversible, metadata-only) driving bulk jobs.

## Infrastructure
- **Transactional outbox** event backbone — at-least-once relay, exponential backoff, dead-letter,
  replay, idempotent producers/consumers.
- **CQRS read model** `rm_command_feed` — async projection, rebuild, freshness watermark, validation.
- **Backend-for-Frontend** — delegated-identity (RFC 8693 OBO) token exchange, circuit breaker,
  partial-failure-tolerant view aggregation, correlation IDs.
- **Streaming gateway** — SSE + polling, ABAC-filtered subscriptions, live revocation,
  critical-priority backpressure, Last-Event-ID recovery.
- **~55 new REST endpoints** under `/api/platform/*` and `/api/bff/*`, `/api/stream/*`.

## Security improvements
- Deny-by-default authorization on every command; ABAC scoping.
- **Hash-chained, tamper-evident** platform audit with a verifier.
- Optimistic concurrency prevents acting on stale state; idempotency prevents double-apply.
- Separation of duties (approvals + break-glass); owner has no unilateral god-tier path.
- Token↔session binding enables live token revocation.
- Delegated downstream identity (no service-principal bypass).
- Permission-filtered live event streams (no existence leak).
- Fleet actions bounded by preview + rings + approval + auto-halt (no synchronous bulk).

## Performance improvements
- Read/stream paths decoupled from the write path via CQRS + outbox (fast reads at scale).
- Bulk execution is ring-batched + rate-limited + durable/resumable (bounded work per tick).
- All new lookups PK/unique-indexed; command-ack path single-row and synchronous.

## Breaking changes
**None.** All migrations additive; all prior endpoints and the 77-test regression unchanged; tokens
without a `jti` remain valid.

## Migration notes
- Apply with `alembic upgrade head` (adds 0004→0006, additive, no downtime).
- Reversible: `alembic downgrade <rev>` (verified up→down→up and full-chain up→base→up).
- Optional env: `OFFLINE_BREAK_GLASS_SECRET` (distinct from `JWT_SECRET`) to enable the offline
  break-glass recovery path; unset ⇒ offline path returns 503 (feature-off, safe default).

## Data model additions
Tables: `command_log`, `approval_requests`, `approvals`, `elevation_grants`, `operator_sessions`,
`outbox`, `read_model_state`, `rm_command_feed`, `segments`, `change_jobs`, `change_targets`.
Additive columns: `operators.{roles,scopes,mfa_enabled}`, `feature_flags.version`,
`platform_audit_log.{correlation_id,command_type,idempotency_key,prev_hash,entry_hash}`,
`customer_refs.{region,version}`.
