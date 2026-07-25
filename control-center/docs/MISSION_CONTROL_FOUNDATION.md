# PFS Mission Control — Foundation (M1–M3) Reference

The permanent foundation of the PFS Control Plane: Operator IAM + Typed Command Pipeline +
hash-chained Audit (M1); Break-glass, Approvals, Sessions, Event Backbone, CQRS, BFF, Streaming
(M2); the Bulk-Operations Safety Engine (M3). This document is the single reference for how the
foundation fits together. It describes behavior already implemented and tested (115 tests green).

> Status: **accepted / frozen.** Modify only for bug fixes. New capability builds *on* these.

---

## 1. Architecture overview

The Control Plane (`control-center/`, a standalone FastAPI service, metadata-only) is the home of
Mission Control. SmokeStack is Data-Plane ERP #1. Every operator mutation is a **typed, authorized,
idempotent, concurrency-checked, audited command**; every fleet action is a **governed
orchestration**; every state change **emits an event** consumed by read models and the live stream.

```
 Operator ─▶ API (console = API client)
                │  typed command / bulk job / governed action
                ▼
        IAM PDP (deny-by-default, ABAC)  ──┐
                │                           │ break-glass (god-tier)     Approvals (M-of-N, SoD)
                ▼                           ▼                              ▼
        Command Pipeline ── executes ──▶ owning service (optimistic concurrency)
                │  hash-chained audit  +  transactional outbox (same tx)
                ▼                                  │
        [ platform_audit_log (chain) ]        [ outbox ] ─relay─▶ projector ─▶ CQRS read model
                                                   │                                │
                                              streaming gateway (ABAC-filtered) ─▶ workspace
        Bulk-Operations Safety Engine ── per target ─▶ Command Pipeline (above)
```

---

## 2. Module relationships

| Module | Responsibility | Depends on |
|---|---|---|
| `iam.py` | Roles, ABAC PDP (`decide`), SoD/break-glass predicates | — |
| `commands.py` | Typed command envelope + pipeline (`dispatch`) | iam, audit_chain, events, breakglass |
| `audit_chain.py` | Hash-chained tamper-evident audit + `verify` | models |
| `approvals.py` | Generic M-of-N/sequential approval workflow | models |
| `breakglass.py` | JIT time-boxed elevation, offline path, recording hooks | approvals, models |
| `sessions.py` | Operator sessions, revocation, presence | models |
| `events.py` | Transactional outbox: emit / relay / DLQ / replay | models |
| `readmodels.py` | CQRS projector + rebuild + freshness (rm_command_feed) | events, models |
| `bff.py` | Delegated-identity (OBO) token + resilient aggregation + circuit breaker | config |
| `streaming.py` | SSE + poll, ABAC-filtered, live revocation, backpressure | iam, sessions, models |
| `changes.py` | Bulk-Operations Safety Engine (segments→rings→halt→rollback) | commands, approvals, events, models |
| `security.py` | Operator auth + session-bound tokens | sessions, models |
| `main.py` | HTTP surface (composition only; logic lives in modules) | all of the above |

Rule: `main.py` holds no business logic beyond wiring; all logic is in the modules; the console is a
client of these same APIs (no UI-only path).

---

## 3. Command flow (M1)

`Command{type, target, tenant/env, params, justification, idempotency_key, expected_version,
correlation_id, approval_policy, approved_by, blast_radius, elevation_id}` → `dispatch()`:

1. **Idempotency** — a prior completed command with this key returns its recorded result (exactly-once).
2. **Authenticate** — operator present (else reject/401).
3. **Authorize** — IAM PDP, deny-by-default; capability + ABAC scope.
4. **SoD / approval** — if the action requires it, an approver ≠ requester with the capability.
5. **Break-glass** — god-tier actions require an active elevation grant (§6).
6. **Execute** — the owning executor re-reads authoritative state and rejects on `expected_version`
   drift (optimistic concurrency). Read models are never the correctness gate; the write path is.
7. **Result-validate + audit** — outcome hash-chained; a `command.*` event emitted to the outbox.

Any missing/failed step **fails closed**.

---

## 4. Approval flow (M2)

`create_request(policy, quorum, reason)` → `decide(approver, approve|reject)`. Invariants: reason
mandatory; **requester can never self-approve**; one vote per approver; approves only on a quorum of
**distinct** approvers; any reject fails it; requests expire. Used by break-glass and bulk jobs.

---

## 5. Break-glass flow (M2)

Normal: `request(capability, reason, quorum)` creates an M-of-N approval + a `pending` grant →
`approve()` by distinct approvers activates a **time-boxed** grant (`expires_at`). The Owner has **no
unilateral god-tier path**. Offline: `open_offline(credential)` validates a separately-held HMAC
emergency credential (distinct secret), activating a grant **without the live approval plane** — for
recovery when the PDP is down; high-severity audited. **Recording failure terminates the grant**
(`on_recording_failure` → revoke). The command pipeline consults `check()` for god-tier actions.

---

## 6. Event flow (M2)

Transactional outbox: `emit()` writes the event in the **same transaction** as the state change
(no dual-write). `relay()` drains pending events to handlers **at-least-once** with exponential
backoff and a terminal **dead-letter** state; producers and consumers are idempotent (unique
`dedupe_key`). `replay()` re-dispatches published events (for projection rebuild). A broker can later
consume the outbox with no producer change.

---

## 7. CQRS flow (M2)

`rm_command_feed` is projected asynchronously from `command.*` outbox events by handlers in
`readmodels.py`. It carries a **freshness watermark** (lag = max outbox id − watermark), is
**rebuildable** (`rebuild` clears + replays), and **validates** (every row maps to an outbox id).
It is a read optimization, not a correctness gate.

---

## 8. Streaming flow (M2)

The gateway serves live events from the outbox over **SSE** (`event_stream`) with a **poll** fallback
(`poll`, Last-Event-ID recovery) — one shared, **ABAC-filtered** path. The filter is pre-computed
from the operator's scope (not a per-event PDP call). Sessions are re-checked each batch (**live
revocation**); **critical events** (security/incident/break-glass/deploy-failed/backup-failed) are
never shed under **backpressure**.

---

## 9. Bulk-Operations flow (M3)

`segment → preview → blast-radius → policy → approval → canary rings → per-target execution →
auto-halt → pause/resume/abort → rollback → completion`, durable via `ChangeJob` + `ChangeTarget`.

- **Segment** — filter expression (erp/region/status/plan/ids…) resolves to a target set.
- **Preview** — dry-run: targets, before/after, conflicts, reversibility, risk, approval, rings. No persist.
- **Blast radius** — single/small/large/cross_region + data-class → approval policy + rings + rollback need.
- **Approval** — destructive/large/cross-region ⇒ M-of-N (reuses §4).
- **Execution** — canary rings, per-tick rate limit, maintenance windows; each target runs as a
  **governed command** (§3): per-target idempotency, optimistic concurrency, **target lease**
  (one job per target). Failures classified; partial success recorded.
- **Auto-halt** — error-budget-per-ring exceeded or **approval revoked mid-flight** → halt.
- **Rollback** — reverse succeeded targets (newest ring first) by re-applying the captured `before`
  state through the pipeline — per-target, idempotent, audited.
- **Mission Control** — `change_jobs-overview` buckets running/paused/halted/aborted/rolled-back/
  awaiting-approval/critical.

---

## 10. Data flow summary

Authoritative state (operators, feature_flags, customer_refs, …) lives in relational tables and is the
source of truth. Derived artifacts — the CQRS feed, the live stream, and Mission-Control roll-ups —
are projections over the **outbox**, rebuildable and never a single point of loss. The audit chain is
append-only and tamper-evident. Tenant metadata only; no customer transactional data in the Control
Plane.

---

## 11. Repository structure (control-center/)

```
main.py            HTTP surface (composition + endpoints; no business logic)
security.py        operator auth, session-bound tokens
iam.py             roles + ABAC PDP + SoD/break-glass predicates
commands.py        typed command envelope + pipeline
audit_chain.py     hash-chained tamper-evident audit
approvals.py       M-of-N / sequential approval workflow
breakglass.py      JIT elevation + offline recovery + recording hooks
sessions.py        operator sessions + presence
events.py          transactional outbox (emit/relay/DLQ/replay)
readmodels.py      CQRS projector (rm_command_feed)
bff.py             delegated identity + resilient aggregation
streaming.py       SSE + poll gateway (ABAC-filtered)
changes.py         bulk-operations safety engine
models.py          SQLAlchemy models (25 tables)
migrations/        Alembic (0001–0006)
tests/             test_control_center.py (77) + test_mission_control{,_m2,_m3}.py (38)
docs/              architecture + phase reports + this foundation doc
```

---

## 12. Migration history

| Rev | Adds | Pattern |
|---|---|---|
| 0001_init | base fleet model (create_all seed) | baseline |
| 0002_feature_flags | feature flags + audit | additive, guarded |
| 0003_dev_platform | lifecycle_stage + dev preview | additive, guarded |
| 0004_mc_foundation | operator roles/scopes, feature_flags.version, audit chain cols, command_log | additive, guarded, reversible |
| 0005_mc_m2 | approvals, approvals, elevation_grants, operator_sessions, outbox, read_model_state, rm_command_feed, operators.mfa_enabled | additive, guarded, reversible |
| 0006_bulk_ops | segments, change_jobs, change_targets, customer_refs.region+version | additive, guarded, reversible |

All of 0004–0006 are additive (expand only; contract deferred), inspector-guarded (safe on fresh +
existing), idempotent, and verified reversible up→down→up (and the full chain up→base→up).

---

## 13. Testing strategy

Hermetic SQLite. `test_control_center.py` (77) is the foundation regression; `test_mission_control*.py`
(38) cover M1–M3 including the mandated adversarial set: self-approval denied, out-of-scope denied,
retried-command no double-apply, stale expected_version rejected, audit-chain tamper detection,
session revocation invalidates token, outbox retry→dead-letter, read-model rebuild, BFF delegated
identity + partial-failure, streaming ABAC filter, bulk lease conflict, approval-revoked-mid-execution,
auto-halt, rollback, large segment, stale version. Every phase gates on **full green regression**,
ruff, and migration up/down/up. **115 tests pass.**

---

## 14. Deployment strategy

Branch `feature/platform-owner-mission-control`; **Development only**, never Production, no merge to
main during foundation build. Promotion path: Claude → Git → Development → automated validation →
human review → Validation env → signed release → controlled Production canary → wider rollout.
Every unfinished capability ships behind a feature flag. Migrations run additively (`alembic upgrade
head`) with no downtime; rollback is `alembic downgrade` (verified) or a code revert.
