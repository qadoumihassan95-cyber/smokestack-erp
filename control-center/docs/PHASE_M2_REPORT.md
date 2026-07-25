# Phase Report — M2: Foundation Depth (governance, event backbone, CQRS, BFF, streaming)

**Phase:** M2 — deepen the platform foundation. **Branch:** `feature/platform-owner-mission-control` (Development only; push is a pending publish step). **Design freeze respected; M1 extended, not modified in behavior.**
**Objective:** implement break-glass + approvals + operator sessions, the transactional outbox event backbone, the first CQRS read model, the delegated-identity BFF, and the streaming gateway — all additive, backward-compatible, tested.

## Existing functionality reused (no duplication)
M1 command pipeline, IAM PDP, hash-chained audit, feature-flag engine, operator auth, additive-migration + inspector-guard pattern. Per the consolidation decision, everything lands in `control-center/` (the permanent Control Plane); no second control plane was created.

## Architecture implemented
1. **Break-glass** (`breakglass.py`): JIT, time-boxed elevation gated by M-of-N approval; **no unilateral owner god-tier**; PDP-independent **offline recovery path** (HMAC emergency credential, distinct secret); **recording hooks + recording-failure-terminates**; revoke/expire. Wired into the command pipeline: god-tier command types (`iam.BREAK_GLASS_REQUIRED`) require an active elevation grant or are rejected `428`.
2. **Approvals** (`approvals.py`): generic M-of-N / sequential workflow; mandatory reason; **requester can never self-approve**; one vote per approver; reject fails; expiry.
3. **Operator sessions** (`sessions.py` + login/`security.py`): session opened at login and bound to the token `jti`; **revoking a session invalidates the live token** (checked in `current_operator`); expiry; MFA/break-glass state; presence. Backward compatible — tokens without `jti` still work.
4. **Transactional outbox** (`events.py`): events written in the same transaction as the state change; at-least-once **relay with exponential backoff, dead-letter** terminal state, **replay**, **idempotent producer** (unique dedupe key) and idempotent consumers; broker-agnostic (a real broker can consume the outbox later with no producer rewrite).
5. **CQRS read model** (`readmodels.py`): `rm_command_feed` projected asynchronously from `command.*` events; **rebuildable** by replay; **freshness watermark**/lag; consistency `validate()`.
6. **BFF** (`bff.py`): **delegated identity via RFC 8693 on-behalf-of token exchange** (never a service principal); resilient view aggregation with **per-source circuit breaker + partial-failure `degraded` flag**; correlation IDs.
7. **Streaming gateway** (`streaming.py`): SSE + polling fallback over the outbox; **ABAC-filtered** per-operator subscriptions; **live session-revocation** mid-stream; **critical-event priority + backpressure**; Last-Event-ID recovery.
8. The command pipeline now **emits `command.*` outbox events** transactionally (feeds read model + streaming).

## Files created
`breakglass.py`, `approvals.py`, `sessions.py`, `events.py`, `readmodels.py`, `bff.py`, `streaming.py`, `migrations/versions/0005_mission_control_m2.py`, `tests/test_mission_control_m2.py`, `docs/PHASE_M2_REPORT.md`.

## Files modified
`models.py` (7 additive tables: approval_requests, approvals, elevation_grants, operator_sessions, outbox, read_model_state, rm_command_feed; `operators.mfa_enabled`); `main.py` (imports; ~20 M2 endpoints; command pipeline break-glass gate; login opens session); `security.py` (jti in token + session-revocation check); `commands.py` (outbox emit + break-glass gate + `elevation_id`); `iam.py` (`BREAK_GLASS_REQUIRED` + `requires_break_glass`).

## Database migrations
`0005_mc_m2` (down_revision `0004_mc_foundation`): additive, inspector-guarded, **reversible** (verified up→down→up). No existing column altered except additive `mfa_enabled`. Company #1 / live SmokeStack unaffected.

## APIs added (~20)
Approvals: `POST /api/platform/approvals`, `/{id}/decide`, `GET /api/platform/approvals`. Break-glass: `POST /breakglass/request`, `/{id}/approve`, `/breakglass/offline`, `/{id}/recording-failed`, `GET /breakglass`. Sessions: `GET /sessions`, `/{id}/revoke`, `GET /presence`. Events: `POST /outbox/relay`, `GET /outbox/dead-letters`. Read model: `GET /readmodels/command-feed`, `/freshness`, `POST /rebuild`. BFF: `GET /api/bff/obo-token`, `/api/bff/mission-control`. Streaming: `GET /api/stream/poll`, `/api/stream` (SSE).

## Events / Permissions / Policies
- **Events:** `command.completed|rejected|failed` (transactional outbox); consumed by the read-model projector and streamed.
- **Permissions:** god-tier command types require break-glass; owner-only for offline break-glass, session revoke, outbox relay, read-model rebuild; internal for the rest.
- **Policies:** SoD (no self-approval) across approvals and break-glass; recording-failure-terminates; deny-by-default preserved.

## Tests added / executed / results
`tests/test_mission_control_m2.py` — 14 tests: approval reason-required + self-approval-forbidden, M-of-N quorum + no-double-vote, break-glass quorum activation + no self-approve, offline credential validation, **recording-failure terminates**, **god-tier command requires break-glass**, **session revocation invalidates token**, command emits outbox + projects read model, **outbox idempotent emit**, **outbox retry→dead-letter**, read-model deterministic rebuild, **BFF OBO delegated identity**, **BFF partial-failure degrades**, **streaming ABAC filter + critical priority**.
**Results:** M2 `14 passed`; M1 mission-control `10 passed`; regression `test_control_center.py` `77 passed`. **Total 101 passing.** `ruff` clean. Migration up/down/up clean.

## Coverage / Performance impact
New paths are single-row, indexed (PK / unique dedupe / jti). The relay is bounded per call and idempotent. Outbox emit adds one insert per command (same transaction). No fleet operations. Within the p99 < 500 ms command-ack and streaming < 2 s targets at this scope.

## Security review
No self-approval (approvals + break-glass); owner has no unilateral god-tier; offline path uses a distinct secret and is high-severity audited; recording failure terminates elevated access; session revocation invalidates tokens; BFF carries the operator's real identity (no service principal); streaming/search-style ABAC filtering on live events; every governance action hash-chained into the audit. No secret is displayed or stored in plaintext.

## Backward-compatibility status
Fully compatible. Additive migration; tokens without jti still valid; all prior endpoints and the 77-test regression unchanged.

## Development deployment status
Not deployed. Staged on the feature branch, Development-only. Push to GitHub + Render Development deploy is an approval-gated publish step.

## Known limitations
- **Relay scheduling:** the outbox relay is invoked on-demand (endpoint) in M2; a scheduled worker/loop (Render worker or APScheduler) is a small infra follow-up so projections update without a manual tick.
- **SSE live-revocation** re-checks by `jti`, but the current `current_operator` dependency doesn't thread the jti onto the operator object, so the SSE path's per-batch revocation uses a best-effort check; the **poll path is fully session-checked every request** (via `current_operator`). Thread jti in M3.
- MFA/SSO are modeled (`mfa_enabled`, `mfa_state`) but not yet enforced against a real IdP (M-later).
- Offline break-glass secret is an HMAC shared secret for M2; hardware-quorum/offline-signed credential is a later hardening step.

## Risks
- Two mutation styles still coexist (M1 pipeline for feature flags; legacy endpoints for other resources) — migrated incrementally per phase, old endpoints kept working.
- Relay-on-demand means read-model lag until a relay runs; mitigated by the freshness endpoint + fail-closed correctness in the command pipeline (read models are not correctness gates).

## Next milestone recommendation
**M3 — Bulk-Operations Safety Engine** (segment → dry-run → blast-radius → approval → canary rings → auto-halt → rollback → per-target idempotency + leases + cross-cell saga), now unblocked by the command pipeline, approvals, events, and read models. Also fold in: scheduled relay worker, jti-threading for SSE, and begin lifting `policy.py`/`tenancy.py` primitives from SmokeStack into shared Control-Plane services.

## Git commit hash
Pending — commit/push to `feature/platform-owner-mission-control` is the approval-gated publish step.
