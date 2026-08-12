# PFS Mission Control — Pre-Implementation Repository Audit & Assessment

**Status:** mandatory first-response assessment before any code. No implementation has begun. Architecture is under design freeze; the four approved docs are the source of truth.
**Method:** the repository was inspected directly (not assumed). Findings below cite real files.

---

## 1. Repository state summary

The repository contains **two distinct Python/FastAPI codebases** plus a large body of docs, tests, and prototypes:

**A. `control-center/` — the standalone PFS Control Center (the current Owner console).**
- `main.py` (1,436 lines): 45 API endpoints — operator auth, ERP products, environments, releases, runtimes + health checks, customers, customer-deployments, deployments, licenses, support sessions, fleet, audit, product overview, home, dashboard, `/api/search`, the full **feature-flag engine** (`/api/platform/*`, deny-by-default `/api/internal/feature-check`), and the **Developer Platform** (`/api/platform/dev/*`, preview sessions, lifecycle stage, rollback).
- `models.py` (264 lines, 14 tables): `operators, erp_products, master_environments, releases, runtimes, customer_refs, customer_deployments, deployments, licenses, support_sessions, feature_flags, feature_flag_audit, dev_preview_sessions, platform_audit_log`.
- `security.py` (55 lines): single JWT (HS256), operator realm, `current_operator`; `main.py` adds `require_internal` (owner/operator/internal) and `require_owner` (owner only).
- `index.html` (1,391 lines): vanilla-JS single-file SPA (shell, global search, notifications, workspace, Developer Mode, feature manager).
- 3 Alembic migrations (`0001_init`, `0002_feature_flags`, `0003_dev_platform`); **77 passing tests**; `render.control.yaml`; deployed independently on Render.

**B. `backend/` — SmokeStack ERP (Data-Plane ERP #1), which already contains substantial platform primitives.**
- `app/routers/`: auth, core, inventory, ledger, hr, users, licenses, partners, chat, telegram, schedule, workflow, control, assistant, attendance.
- **Platform primitives already built:** `tenancy.py` (212 lines — `company_id` scoping, tenant-context session tagging, automatic query scoping, **impersonation-token minting**, system context), `policy.py` (320 lines — a **layered policy engine**: Platform → Company status → Subscription → App → Module → Feature flag → Permission → Branch), `permissions.py` (RBAC role→perm map), `idempotency.py`, `locking.py` (global lock ordering), `observability.py` (structured logging with request/company/user context), `counters.py`, `company_config.py`.
- `app/pfs/` — an **embedded PFS control-plane sub-app** (`auth/login`, `auth/me`, `overview` — thin) with its own `permissions.py`, `repository.py`, `security.py`, `seed.py`.
- `app/platform/registry.py` — business-agnostic **app/module registration** (`AppDescriptor`, `ModuleSpec`, dependency keys); `app/apps/` (smoke_shop, catalog).
- 44 tests; 32 migrations; `render.yaml` + `render.staging.yaml`; CI at `.github/workflows/{ci.yml, deploy.yml, control-center-ci.yml}`.

**Key finding:** significant Mission-Control substrate already exists in **backend/app** (tenancy, layered policy engine, idempotency, locking, RBAC, impersonation, observability, app registry) — but it lives in the ERP codebase, while the Owner console lives in the separate **control-center** service. There is partial duplication of the "control plane" concept across the two. Resolving that is the single most consequential decision before building (see §11 and the decision requested at the end).

---

## 2. Existing architecture summary (as-built vs approved)

| Approved concept | As-built today | Where |
|---|---|---|
| Two-plane (Control vs Data) | **Present in spirit:** control-center is a separate service/DB/realm; SmokeStack is the ERP. But backend/app/pfs re-implements a control plane inside the ERP. | control-center/ + backend/app/pfs |
| Operator realm, separate from tenants | Present (control-center JWT realm; backend user realm) | security.py (both) |
| RBAC | Present but **coarse**: operator `platform_role ∈ {owner, operator, internal}`; ERP RBAC is rich (role→perm map) | control-center/main.py; backend/app/permissions.py |
| ABAC scoping | **Absent** for operators; ERP has branch scoping | — |
| Multi-tenancy (company scoping) | Present for the ERP (company_id + auto-scoping + impersonation) | backend/app/tenancy.py |
| Layered policy engine | **Present** (platform→…→permission→branch) | backend/app/policy.py |
| Feature flags (deny-by-default) | **Present & strong** (visibility levels, targeting, rollout, audit, customer=prod enforcement) | control-center/main.py |
| Developer Mode / preview / lifecycle / rollback | **Present** | control-center/main.py |
| Support sessions (metadata-only, revocable) | Present (pending_erp_integration); impersonation-token minting in backend | control-center + backend |
| Audit log | Present (`platform_audit_log`) but **not hash-chained / not tamper-evident** | control-center/models.py |
| Idempotency / locking | **Present** (framework) | backend/app/{idempotency,locking}.py |
| Observability | Structured logging w/ context; **no OTel/TSDB/traces** | backend/app/observability.py |
| Migrations (additive, expand/contract) | **Practiced** (WaveB composite-key expand/contract already done) | backend/migrations |
| CI/CD | Present (3 workflows) | .github/workflows |
| Command pipeline, event backbone, CQRS read models, BFF, streaming, cells/residency, Decision Center, Digital Twin, Knowledge Center | **Absent** (designed, not built) | — |

---

## 3. Gap matrix — approved Mission Control capabilities

Status legend: **C** Complete · **P** Partial · **M** Missing · **X** Implemented incorrectly/duplicated · **Mig** Needs migration · **H** Needs hardening · **Dep** Blocked by dependency.

| # | Capability | Status | Evidence / note |
|---|---|---|---|
| Foundation | Operator IAM Org (10 roles, ABAC, SoD, MFA, SSO, JIT elevation, break-glass, M-of-N, session recording, delegation, offline recovery) | **P/M** | Have single realm + 3 coarse roles + JWT. Everything else missing. Highest priority. |
| Foundation | Typed Command Pipeline (authz→policy→blast-radius→approval→exec→revalidate→audit, idempotent) | **M** | Ingredients exist (idempotency.py, policy.py, audit) but no unified pipeline. |
| Foundation | Tamper-evident audit (hash-chained, WORM) | **P/H** | `platform_audit_log` exists; not hash-chained. |
| Foundation | Event backbone (outbox, broker, DLQ, replay, versioning) | **M** | None. |
| Foundation | BFF + delegated identity (token exchange) | **M** | Console calls API directly; no BFF, no on-behalf-of. |
| Foundation | CQRS read models (per-Center projections, freshness, rebuild) | **M** | Direct queries today. |
| Foundation | Streaming gateway (SSE/WS, ABAC-filtered, live revocation) | **M** | None. |
| P2 | Bulk-Operations Safety Engine (segment→dry-run→rings→halt→rollback, idempotency, leases, saga) | **M** | Control-center has bulk *selection UI* only; no engine. |
| P3 | Mission Control (attention queue, two-tier signals, intelligence-degraded) | **P/Dep** | Have dashboard widgets + health-check-all; Decision Center not built. |
| P4 | Universal search (event index, ABAC trim of results+counts+timing, graph, zero-knowledge escalation) | **P/Mig** | `/api/search` is in-process over control-center metadata; not permission-trim-hardened, no index. |
| P5 | Customer 360 (full profile incl. cost/margin/usage/security/compliance/backups/tickets) | **P** | Have overview + customer drawer; many panels missing. |
| P7 | Deployment & Release Center (signed artifacts, canary/ring/blue-green, adoption, cell/region) | **P** | Have releases/runtimes/deployments/health + flag rollback; no signed-artifact pipeline. |
| P8 | Remote Ops (consent lifecycle, in-region hash-chained recording, governed palette, recording-fail-terminates, presence/leases) | **P/H** | Have support sessions + impersonation token; no recording/consent/palette. |
| P9 | Incident Command | **M** | None. |
| P10 | Notifications / On-call / Escalation (multi-channel, routing, ack deadlines) | **P** | Have Telegram transport + reminders + notification UI; no routing/on-call/escalation. |
| P11 | Security Center (failed logins, threat, keys/certs, quarantine) | **M** | None as a surface. |
| P12 | Compliance Center (installable packs, effective-dating, citations, regulatory-change) | **M** | Only research docs; feature-check ≠ compliance packs. |
| P13 | Data Governance / DSAR / crypto-erasure / legal hold | **M** | None. |
| P23 | Super Admin god-tier (per-operation classification, break-glass, kill switch, maintenance) | **P** | Have dev tools (metadata-only) + support sessions; no god-tier ops/classification. |
| P14 | AI Control Center (agent registry, model/cost/latency/hallucination, budgets) | **M** | SmokeStack has an assistant router; no PFS AI runtime. |
| P15 | Integration Center (registry, credential health, sync/logs) | **P** | Telegram worker + partners + payment-monitoring; no unified registry/surface. |
| P16 | Marketplace (versioned modules, entitlements, per-tenant install/upgrade/rollback, 3rd-party review) | **P** | `platform/registry.py` gives app/module registration; no versioning/entitlement/lifecycle. |
| P17 | Observability (OTel + backend, business SLOs, correlation) | **P** | Structured logging only. |
| P18 | Knowledge Center (versioned, cited, effective-dated, citation gate) | **M** | Static docs only. |
| P19 | Platform Intelligence / Decision Center | **M** | Designed, not built. |
| P20 | Audit/Forensics/eDiscovery (explorer, hash verification, holds) | **P/H** | Audit data exists; no explorer/verification/holds. |
| P21 | Runbooks / Automation (governed playbooks) | **M** | None. |
| P22 | Trust / Status / SLA | **M** | None. |
| P24 | PFS Business Control (revenue, cost, margin, subscription/usage/support economics, commercial intel) | **M** | None. |
| P25 | FinOps / Quotas / Rate-limits | **M** | None. |
| P26 | Customer Success / Lifecycle | **M** | None. |
| P27 | Policy-as-code guardrails (versioned, testable, in-pipeline, freeze windows, thresholds) | **P** | `policy.py` is a strong base; not versioned-guardrail/freeze-window/threshold-aware. |
| P28 | API/CLI/IaC parity | **P** | API + render IaC exist; no CLI; parity not formalized. |
| Base | Cells + residency + tenant catalog (tiered tenancy) | **P/Dep** | company_id scoping exists; no cells/catalog/residency enforcement. |
| Base | Migrations (additive, expand/contract) | **C** | Practiced already. |
| Base | Test harness | **P** | 77 + 44 tests; missing the adversarial matrix (self-approval, count-leak, revocation, double-apply, stale-command, recording-fail, unsigned-release, ledger-immutability, cross-region). |

**Net:** the *foundations* (IAM-org, command pipeline, hash-chained audit, event backbone, BFF, read models, streaming) are the gating gap — almost everything else is Partial-or-Missing **because it depends on them**. Encouragingly, strong reusable primitives already exist (policy engine, tenancy/impersonation, idempotency, locking, feature flags, registry) — this is additive evolution, not a rewrite.

---

## 4. Dependency graph (build order is forced by this)

```
        ┌────────────────── FOUNDATION (everything depends on these) ──────────────────┐
        │  Operator IAM Org ── Typed Command Pipeline ── Hash-chained Audit             │
        │        │                    │                        │                        │
        │        │             Event Backbone (outbox→broker)  │                        │
        │        │                    │                        │                        │
        │   BFF (delegated id) ── CQRS Read Models ── Streaming Gateway (ABAC-filtered)  │
        └───────┬───────────────┬───────────────┬───────────────┬───────────────┬──────┘
                ▼               ▼               ▼               ▼               ▼
        Bulk-Ops Engine   Universal Search  Customer 360   Deployment Ctr   Remote Ops
                │               │                                │               │
                ▼               ▼                                ▼               ▼
         (all mutating       Incident Cmd ── Notifications/On-call        Super Admin (god-tier)
          Centers)                │
                                  ▼
   Decision Center ──▶ Mission Control (Tier-1 ranking) + raw signals (Tier-2 fallback)
   Knowledge Center ──▶ Compliance Center ; Data Governance/DSAR ; AI Control ; Marketplace ;
   Observability ; FinOps/PFS Business ; Trust/SLA ; Runbooks ; Policy guardrails ; API/CLI parity
```

Rule: **no mutating Center is built before the command pipeline exists**; **no ranked Mission Control before a raw-signal fallback exists**; **no cross-tenant view before ABAC + residency exist.**

---

## 5. Recommended implementation milestones (phased, each shippable & reversible)

- **M0 — Consolidation decision + branch + CI wiring** (this assessment; the decision requested below).
- **M1 — Foundation slice:** Operator IAM (roles+ABAC skeleton, SoD, deny-by-default) + Typed Command Pipeline + Hash-chained Audit, proven end-to-end on **one** existing mutation. *(This is the first vertical slice, §11.)*
- **M2 — Foundation depth:** JIT elevation + break-glass (M-of-N, offline path) + session model; Event Backbone (outbox); first CQRS read model; BFF with delegated identity.
- **M3 — Bulk-Operations Safety Engine** (segment→dry-run→rings→halt→rollback, idempotency, leases, saga).
- **M4 — Mission Control** (raw-signal Tier-2 first, then Decision-Center Tier-1) + Streaming gateway + Notifications/On-call.
- **M5 — Universal Search** (event index + ABAC trim) + **Customer 360** depth.
- **M6 — Deployment/Release Center** (signed artifacts) + **Remote Ops** (recording) + **Incident Command**.
- **M7 — Governance tier:** Compliance Center, Security Center, Data Governance/DSAR, Super Admin god-tier classification, Policy-as-code guardrails.
- **M8+ — High tier:** AI Control, Integration, Marketplace, Observability (OTel), Knowledge, Platform Intelligence, Audit/Forensics, FinOps, PFS Business Control, Runbooks, Trust/SLA, API/CLI parity.
- **Medium/Future:** Customer Success, Quotas, Localization, Digital Twin/what-if, Mobile companion, predictive recommendations.

This matches the priority list in the brief; the only reorder is building Mission Control's **raw-signal fallback before** its Decision-Center ranking, so "intelligence degraded ≠ all clear" holds from day one.

---

## 6. Files & services likely affected (first milestones)

- **New (foundation):** `iam/` (roles, ABAC PDP, elevation, break-glass, sessions), `commands/` (command envelope, pipeline, dispatch, result-validation), `audit/` (hash-chained append + verifier), `events/` (outbox + relay), `readmodels/`, `bff/` (or a BFF layer in the console). New Alembic migrations for `operators` (add attributes: roles/scopes/mfa), `elevation_requests`, `break_glass_sessions`, `approvals`, `commands`, `audit_chain`, `outbox`.
- **Modified:** `control-center/security.py` (ABAC PDP, delegated identity), `control-center/main.py` (route one mutation through the pipeline; keep others working), `control-center/models.py` (new tables), `index.html` (break-glass banner, command result surfacing, permission-aware nav), CI workflow(s).
- **Reused (not rewritten):** `backend/app/policy.py` (layered policy → guardrail engine), `backend/app/idempotency.py`, `backend/app/locking.py`, `backend/app/observability.py`, `backend/app/tenancy.py` (impersonation/consent patterns), `platform/registry.py` (marketplace base), the feature-flag engine.

---

## 7. Database & migration strategy

Additive, **expand → backfill → dual-read/write → validate → switch → contract-later** (already practiced in this repo, e.g., WaveB composite keys). Rules: no destructive change without a rollback plan; backup before risky migration; every migration idempotent, inspector-guarded (as `0002/0003` already are), reversible, and tested up→down→up on SQLite *and* against realistic volume; per-tenant/cell awareness where relevant; integrity checks post-migrate; migration progress observable. New foundation tables are purely additive, so Company #1 / the live SmokeStack tenant is unaffected (the policy engine already evaluates Company #1 → ALLOW).

---

## 8. Testing strategy

Every phase ships with: unit, integration, API, **permission + ABAC-scope + negative-authorization**, idempotency, concurrency, migration up/down, rollback, audit (incl. **hash-chain verification**), event-delivery/retry, failure-injection, security, UI (jsdom), accessibility, and **full regression of existing control-center (77) + SmokeStack (44) suites**. Plus the mandated **adversarial suite**: operator-cannot-self-approve; cannot-read-outside-scope; search-doesn't-leak-counts/timing; streaming-access-revoked-live; bulk-jobs-cannot-overlap-target; retried-jobs-don't-double-apply; stale-projection-cannot-execute-unsafe-command; recording-failure-terminates-session; production-rejects-unsigned-release; ledger-cannot-be-updated/deleted; AI-cannot-bypass-deterministic-rules; tenant-data-doesn't-cross-region; offline-recovery-is-auditable; Decision-outage-≠-all-clear. Green regression is a gate for every phase.

---

## 9. Deployment strategy

Branch **`feature/platform-owner-mission-control`** off the current control-center line; never on main; no Production deploy without explicit approval. Flow per the approved chain: **Claude → Git → Development → automated validation → human review → Validation env → signed release → controlled Production canary → health verify → wider rollout**. Each completed phase is a separate, meaningfully-messaged commit (no giant commit, no force-push, no history rewrite). Every unfinished module ships behind a feature flag. Delivery to GitHub is via the browser-upload flow and is an explicit publish action requiring your go-ahead.

---

## 10. Risk register

| Risk | Sev | Mitigation |
|---|---|---|
| **Two overlapping control planes** (control-center vs backend/app/pfs) diverge | High | Resolve now (decision below); one home, the other reduced to reference/data-plane |
| Foundation (IAM/pipeline/audit) is large and gates everything | High | Thin vertical slice first (§11), additive, flag-guarded, one mutation at a time |
| Regression to the **live SmokeStack business** / Company #1 | High | Additive-only migrations; policy engine already ALLOWs Company #1; full regression gate |
| Coarse operator roles today → ABAC retrofit touches every endpoint | Med | Introduce PDP centrally; migrate endpoints incrementally behind a default-allow-for-owner compatibility shim, tightening per phase |
| Audit not yet tamper-evident | Med | Hash-chain in M1; verifier test; WORM later |
| Scope is enormous (28 phases) | Med | Strict phase gates + phase reports; no next phase on critical failure |
| Delivery can't reach Render/GitHub from sandbox | Low | Browser-upload flow with your approval; Dev-first, never Prod |
| Secrets handling | Med | References only; never plaintext; reuse secret-manager pattern |

---

## 11. First production-grade vertical slice (recommended)

**Slice: "Every mutation is a governed command" — the Foundation triad on one real action.**

Scope, end-to-end and thin:
1. **Operator IAM skeleton** — introduce the role set + an **ABAC Policy Decision Point** (`decide(operator, action, target, context) → allow/deny + reason`), deny-by-default, with a compatibility shim so existing owner flows keep working while new checks tighten. Add `operators` attributes (roles[], scopes) via additive migration.
2. **Typed Command Pipeline** — a `Command` envelope (`type, operator, target, tenant/env context, params, justification, idempotency_key, expected_version, correlation_id, requested_at, approval_policy, blast_radius`) and a dispatcher implementing the mandated lifecycle: **authenticate → authorize (PDP) → policy check → blast-radius classify → (approval if required) → execute via the owning service with expected-version revalidation → result-validate → hash-chained audit**. Reuse `idempotency.py`/`locking.py`.
3. **Hash-chained audit** — extend `platform_audit_log` (additive) into a tamper-evident chain (each record commits to the prior hash) + a verifier.
4. **Prove it on one existing mutation** — route **feature-flag enable/disable** (already the safest, most-tested mutation) through the pipeline: ABAC-gated, idempotent, expected-version-checked, hash-chained-audited. Leave every other endpoint untouched and working.
5. **Tests** — unit + API + the first adversarial tests (self-approval denied for a gating change, cannot-act-outside-scope, retried-command-doesn't-double-apply, stale-expected-version-rejected, audit-chain-verifies), plus full regression of the existing 77 tests.

Acceptance criteria: the chosen mutation only succeeds through the pipeline; deny-by-default proven; idempotency + optimistic concurrency proven; audit chain verifies; **zero regressions**; behind a feature flag; reversible.

---

## 12. Why that slice must come first

- **It is the dependency root** (§4): every mutating Center — bulk ops, deployments, remote ops, super admin, compliance — must flow through this exact pipeline. Building any Center first would either bypass the safety architecture (forbidden) or require rework.
- **It de-risks the hardest invariants early** on real code: deny-by-default authorization, idempotency, optimistic-concurrency revalidation ("read models aren't correctness gates"), and tamper-evident audit — the four properties the whole brief hinges on.
- **It is additive and low-blast:** it wraps one already-tested action behind a flag, changes no business behavior, and is fully reversible — the safest possible first change to a live platform.
- **It compounds:** once the pipeline exists, every subsequent phase inherits authorization + idempotency + audit for free, which is what makes a 28-phase build tractable without accumulating unsafe one-offs.

---

## Decision required before coding

The audit found **two overlapping "control plane" implementations**: the standalone **`control-center/`** service (the current Owner console, separate DB/realm, independently deployed — matches the two-plane invariant) and an **embedded `backend/app/pfs` + `backend/app/platform`** control-plane inside the SmokeStack ERP (with the stronger primitives: `tenancy.py`, `policy.py`, `idempotency.py`, `locking.py`, `registry.py`). The Mission Control workspace must have **one home**, and I must not build twice or let them diverge further. My recommendation: **make `control-center/` the workspace's home (the PFS control plane), and lift the reusable primitives from `backend/app` into it as shared libraries**, leaving SmokeStack as pure Data-Plane ERP #1. But this is genuinely your call because it affects deployment topology, and an alternative is to consolidate the control plane *into* the backend platform layer where those primitives already live. That decision is the one thing I want to confirm before writing the first slice.
