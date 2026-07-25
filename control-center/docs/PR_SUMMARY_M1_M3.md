# Pull Request — PFS Mission Control Foundation (M1 + M2 + M3)

**Branch:** `feature/platform-owner-mission-control` → (review) · **Target:** not main; Development only.
**One foundational PR** covering the three accepted milestones that form the permanent PFS Control-Plane
foundation. Additive, backward compatible, design-frozen.

## Architecture implemented
- **M1 — Operator IAM + Typed Command Pipeline + Hash-Chained Audit.** Deny-by-default ABAC PDP;
  every mutation is a typed, idempotent, optimistic-concurrency-checked, audited command; tamper-evident
  audit chain with verifier. Proven on the feature-flag mutation.
- **M2 — Foundation depth.** Break-glass (JIT/M-of-N/offline/recording-terminates), generic approval
  workflow (SoD), operator sessions (revocable), transactional outbox (relay/DLQ/replay), first CQRS
  read model, delegated-identity BFF (RFC 8693) with circuit breaker, ABAC-filtered streaming gateway.
- **M3 — Bulk-Operations Safety Engine.** The only path for fleet actions: segment → preview →
  blast-radius → approval → canary rings → per-target governed execution → auto-halt → pause/resume/
  abort → rollback → Mission-Control roll-up. Each per-target change runs through the M1 pipeline.

## Files changed
- **New modules (11):** `iam.py`, `commands.py`, `audit_chain.py`, `approvals.py`, `breakglass.py`,
  `sessions.py`, `events.py`, `readmodels.py`, `bff.py`, `streaming.py`, `changes.py`.
- **Modified (3):** `main.py` (composition + endpoints), `models.py` (additive tables/columns),
  `security.py` (session-bound tokens).
- **Migrations (3):** `0004_mc_foundation`, `0005_mc_m2`, `0006_bulk_ops`.
- **Tests (3):** `test_mission_control.py`, `_m2.py`, `_m3.py`.
- **Docs:** foundation reference, ADR index, migration review, changelog, three phase reports,
  the pre-implementation assessment.

## Database changes
11 new tables; additive columns on `operators`, `feature_flags`, `platform_audit_log`,
`customer_refs`. All additive, inspector-guarded, idempotent, reversible (see `MIGRATION_REVIEW_M1_M3.md`).
No destructive change; no backfill; live data unaffected.

## APIs
**86** total endpoints (from 45 pre-foundation) — new surfaces: `/api/platform/commands`,
`/approvals/*`, `/breakglass/*`, `/sessions/*`, `/outbox/*`, `/readmodels/*`, `/change-jobs/*`,
`/segments/*`, `/audit/verify`, `/api/bff/*`, `/api/stream/*`. All REST; console-is-API-client; no
UI-only logic; god-tier is API-triggerable but human-gated.

## Tests / coverage
**115 tests, all passing:** 77 foundation regression (unchanged) + 38 Mission-Control (10 M1 / 14 M2 /
14 M3). Adversarial coverage: self-approval denied, out-of-scope denied, retried-command no double-apply,
stale-version rejected, audit-chain tamper detection, session revocation, outbox retry→dead-letter,
read-model rebuild, BFF delegated identity + partial failure, streaming ABAC filter, bulk lease conflict,
approval-revoked-mid-execution, auto-halt, rollback, large segment, stale version. Lint (ruff) clean;
`py_compile` clean; migrations up/down/up + full-chain verified.

## Security
Deny-by-default; hash-chained audit; optimistic concurrency + idempotency; SoD (approvals + break-glass);
owner has no unilateral god-tier; token↔session revocation; delegated downstream identity (no
service-principal bypass); permission-filtered streams; fleet actions bounded by preview/rings/approval/
auto-halt. Metadata-only control plane preserved.

## Performance
Reads/streams decoupled from writes (CQRS + outbox); bulk execution ring-batched + rate-limited +
resumable; all new lookups indexed; command-ack single-row synchronous. Designed to scale toward
tens of thousands (cross-cell saga + scheduled workers land in later milestones).

## Known limitations
- Outbox relay and bulk execution are **tick/endpoint-driven** in the foundation; a scheduled worker is
  a small M4 infra follow-up.
- **SSE** live-revocation best-effort (jti not yet threaded onto the operator object); the **poll** path
  is fully session-checked per request.
- MFA/SSO modeled but not yet enforced against a real IdP; offline break-glass uses an HMAC shared
  secret (hardware-quorum later).
- Cross-cell saga deferred until the tenant catalog / cells exist.

## Future roadmap
M4 Mission Control (attention queue over the raw-signal tier first) → Universal Search → Customer 360 →
Deployment/Release Center → Remote Ops → Incident Command; plus the scheduled worker, jti-threading,
and progressively lifting SmokeStack platform primitives (`policy.py`, `tenancy.py`) into shared
Control-Plane services. No second control plane is created (consolidation decision is final).
