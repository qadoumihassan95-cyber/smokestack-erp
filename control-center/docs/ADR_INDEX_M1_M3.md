# ADR Index — Mission Control Foundation (M1–M3)

Architecture Decision Records realized in the implemented foundation. These are *implemented and
tested* decisions (distinct from the design-doc ADRs A/P/G/K/D/T/W/X, which they are consistent with).
Status: **Accepted** unless noted.

| # | Decision | Chosen | Rationale | Milestone |
|---|---|---|---|---|
| MC-1 | Operator authority | Least-privilege roles + ABAC PDP, deny-by-default; owner passes the PDP (holds wildcard, not a bypass) | No god-login; every check server-side | M1 |
| MC-2 | Every mutation | Typed, idempotent command through one pipeline (authz→policy→blast→approval→execute→audit) | Uniform SoD/audit; API parity; no one-offs | M1 |
| MC-3 | Correctness under stale reads | Executor re-validates authoritative state via `expected_version` (optimistic concurrency); read models are not gates | Prevents acting on stale projections | M1 |
| MC-4 | Idempotency | Unique idempotency key ⇒ exactly-once; retries/replays safe | Safe resume, no double-apply | M1 |
| MC-5 | Audit | Append-only, **hash-chained**, verifiable | Tamper-evident; forensics | M1 |
| MC-6 | Approvals | Generic M-of-N/sequential; **requester never self-approves**; one vote/approver; expiry | Separation of duties | M2 |
| MC-7 | Break-glass | JIT, time-boxed elevation gated by M-of-N; **owner has no unilateral god-tier** | Insider-risk control | M2 |
| MC-8 | Break-glass recovery | PDP-independent **offline path** (distinct HMAC secret) + escalation | Recoverable when the auth plane is down | M2 |
| MC-9 | Elevated recording | **Recording failure terminates** the grant | No unrecorded god-mode | M2 |
| MC-10 | Sessions | Token bound to a revocable server session (`jti`); tokens without jti stay valid | Live token revocation, backward compatible | M2 |
| MC-11 | Event backbone | **Transactional outbox** (event in the same tx), at-least-once relay + dead-letter + replay | No dual-write; broker-agnostic; durable | M2 |
| MC-12 | Read models | CQRS projections over the outbox; rebuildable; freshness watermark | Fast reads decoupled from write path | M2 |
| MC-13 | BFF identity | **Delegated identity via token exchange (RFC 8693 OBO)**, never a service principal | ABAC enforced downstream; no BFF-bypass | M2 |
| MC-14 | View aggregation | Per-source circuit breaker + partial-failure `degraded` flag | Resilient views; no hard-fail | M2 |
| MC-15 | Streaming | One ABAC-filtered path (SSE + poll); live session revocation; critical-priority backpressure | No existence leak; recovery; safety | M2 |
| MC-16 | Fleet actions | **Only** via the Bulk-Operations Safety Engine — segment→preview→rings→halt→rollback | No synchronous bulk buttons; bounded blast | M3 |
| MC-17 | Bulk blast radius | Deterministic classification drives approval policy + canary rings + rollback need | Right control for the scope | M3 |
| MC-18 | Bulk correctness | Per-target idempotency + optimistic concurrency + **one job per target (lease)** | No double-apply, no overlapping jobs | M3 |
| MC-19 | Bulk safety | Error-budget auto-halt; approval-revoked-mid-flight halt; pause/resume/abort; per-target rollback | Fail-safe orchestration | M3 |
| MC-20 | Migrations | Additive, inspector-guarded, idempotent, reversible (expand now, contract later) | Zero-downtime, safe on live DB | M1–M3 |

Cross-cutting invariants held by all: deny-by-default, fail-closed on gates, tamper-evident audit,
metadata-only control plane, console-is-API-client, no UI-only logic.
