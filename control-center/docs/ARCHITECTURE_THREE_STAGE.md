# PFS Platform Architecture — Intelligent Three-Stage Development System

**Document type:** Chief-Architect design blueprint (RFC / target architecture)
**Status:** Proposed. Supersedes nothing; extends the Control-Plane/Data-Plane redesign, the Platform Governance v1.0, and the Feature Management + Developer Platform milestones already shipped.
**Audience:** Platform Owner + future platform engineering team.

> Framing: PFS is not an ERP. PFS is the platform that *owns* ERPs — the way QuickBooks Accountant owns client books, App Store Connect owns apps, Vercel owns deployments, and Stripe Dashboard owns money movement. This document chooses the industry-standard architecture at every fork and justifies the choice. Where the described design can be improved on, it says so and explains why.

---

## 0. Executive summary

PFS is a **control plane** that governs many independent ERP **products** across three physically isolated **stages** — Development Lab, Compliance & Validation, and Customer Production — connected only by a one-way, artifact-centric **promotion pipeline**. Code and configuration flow *upward* as immutable, signed releases through mandatory gates; synthetic data seeds *downward*; **customer production data never flows down**. Every runtime capability is governed by a deny-by-default **feature-flag plane** so features are exposed without redeploying. Tenancy uses the **pool-by-default / silo-when-required** SaaS model with Postgres row-level security and a control-plane tenant catalog that supports live migration between shared and dedicated databases. Compliance is a **first-class stage** built on pluggable, versioned, jurisdiction-scoped rule packs. Security is enterprise-grade throughout: RBAC+ABAC, signed releases with SLSA provenance, per-stage secret isolation, fully-audited impersonation, and tenant isolation enforced in the database. An **AI orchestration layer** is designed as a first-class but *ordinary* consumer of the same governed APIs — no backdoors — so agents can be added without redesign. Everything is event-driven and adapter-based to keep the ERP marketplace, plugin marketplace, country modules, tax engines, payment/bank providers, and AI providers pluggable.

The design is deliberately an **evolution of the existing PFS Control Center**, not a rewrite. Sections 15–16 map each component to what already exists and give a phased roadmap.

---

## 1. First principles (non-negotiable invariants)

These constraints bind every downstream decision. When a feature and an invariant conflict, the invariant wins.

1. **Two planes.** The Control Plane (PFS) governs; the Data Plane (ERP runtimes) executes. PFS stores *metadata and control state only* — never customer transactional data.
2. **Three isolated stages.** Development, Compliance & Validation, and Production never share a database, credentials, or network trust boundary. They communicate only through signed artifacts and explicit, audited APIs.
3. **One-way promotion.** Code moves up through gates; data (synthetic) seeds down. **Real customer data never descends** to a lower stage. This is a privacy and blast-radius invariant, not a convenience.
4. **Deny-by-default everywhere.** Feature access, API authorization, tenant data access, and promotion all fail closed. A missing or ambiguous rule denies.
5. **Immutable, signed releases.** Nothing reaches Production unsigned. Every artifact is content-addressed, versioned (SemVer), and carries provenance. Rollback is always possible because prior artifacts are immutable and retained.
6. **Backend-enforced authority.** No trust in frontend state, query params, or client headers. Owner/developer/tenant authority is validated server-side; the UI only hides.
7. **Tenant isolation is defense-in-depth.** Application scoping *and* database row-level security *and* the tenant catalog must all agree. A bug in one layer must not leak data.
8. **Everything is auditable and reversible.** Every privileged action is recorded with actor, before/after, and reason; every state change has an inverse or a compensating action.
9. **Modularity over hardcoding.** Workflows, jurisdictions, providers, and ERP products are adapters behind stable ports. Adding one must not require editing the core.

---

## 2. Logical architecture

```
                         ┌──────────────────────────────────────────────┐
                         │                 CONTROL PLANE (PFS)            │
                         │  Identity · Tenant Catalog · Product Registry  │
                         │  Feature-Flag Service · Release Registry       │
                         │  Promotion Orchestrator · Compliance Gate      │
                         │  Audit/Event Backbone · Secrets Broker         │
                         │  Developer Workspace · AI Orchestration        │
                         └───────────────┬───────────────┬──────────────┘
              signed releases ↓ (up-only promotion)      │ governs / observes
        ┌───────────────────────┬───────────────────────┬───────────────────────┐
        │   STAGE 1: DEV LAB    │  STAGE 2: COMPLIANCE   │  STAGE 3: PRODUCTION   │
        │  (ephemeral, owner)   │   & VALIDATION (gate)   │  (customers, signed)  │
        │  synthetic data only  │  prod-like, synthetic   │  real tenant data     │
        └───────────────────────┴───────────────────────┴───────────────────────┘
             DATA PLANE — independent ERP runtimes per product × per stage
```

The Control Plane is a set of cooperating services (logically; they may start as modules of one deployable and be extracted as scale demands — see §17). The Data Plane is many ERP runtimes, each an independent product, each existing separately in every stage.

**Recommended decomposition (ports & adapters / hexagonal):** the platform core depends on *ports* (interfaces) — `ReleaseStore`, `FlagEvaluator`, `TenantResolver`, `CompliancePack`, `SecretProvider`, `EventBus`, `ModelProvider`. Concrete adapters implement them. This is what keeps tax engines, banks, AI providers, and ERP products pluggable and is the single most important structural decision in the document.

---

## 3. The three stages in depth

Each ERP product exists **three times** — once per stage — as an independent runtime with its own database and credentials. A stage is a trust boundary, not a folder.

### Stage 1 — Development Lab (the owner's laboratory)
Purpose: build and break safely. Owner-only; invisible to customers by construction (it has no customer tenants and no route into Production).

Guarantees the architecture must provide: full editability; **resettable databases** (drop/recreate on demand); **synthetic data factories** (seeded, deterministic, PII-free); migration dry-runs; stress/load harness; simulated multi-user sessions; profiling and tracing turned up to maximum; unrestricted feature-flag toggling. Because nothing here is customer data, the lab can be aggressive: destructive operations are first-class, not guarded.

Isolation: separate database cluster/namespace, separate secrets, no network path to Compliance or Production data. The only *output* of the lab is a candidate **build artifact** submitted to the pipeline.

### Stage 2 — Compliance & Validation (the gate)
Purpose: prove a candidate is correct and lawful before it can be signed for Production. This stage is **production-like** (same engine versions, representative-but-synthetic data volumes) and is where automated validation runs. It is a *gate*, not a playground: it is driven by the pipeline, not by hand-editing.

It runs three families of checks (§8): **engineering** (API regression, permissions, security, performance, migration validation, data-consistency), **accounting invariants** (double-entry integrity, GL balancing, inventory valuation under FIFO/LIFO/Weighted-Average, financial-statement generation, bank reconciliation), and **jurisdiction compliance** (US sales tax, payroll tax, IRS rules today; CA/EU/ME as pluggable packs). Promotion is blocked unless all applicable suites pass; approval is automated-first with an optional human sign-off gate for regulated changes — never human-only.

### Stage 3 — Customer Production
Purpose: run only stable, **signed** releases for real tenants. Each customer may run a *different* version, license, feature set, and module set — all controlled from PFS. Rollback is always available. Deployments use rings, canaries, and blue/green with automatic rollback on SLO breach (§9).

### Data-flow rules between stages (the crux)

| Flow | Direction | Allowed? | Mechanism |
|---|---|---|---|
| Build artifact / release | Dev → Compliance → Prod | Yes (only path up) | Signed, content-addressed artifact + provenance |
| Feature-flag config | Control plane → any stage | Yes | Flag service, stage-scoped, deny-by-default |
| Synthetic/anonymized fixtures | Prod schema → down as *shape only* | Yes | Schema + generator; **no row values** copied |
| **Real customer data** | Prod → Compliance/Dev | **Never** | Hard-blocked; privacy invariant |
| Migration script | Dev → Compliance (validated) → Prod | Yes | Runs in Compliance against synthetic prod-shape data first |
| Direct DB connection across stages | any | Never | No cross-stage credentials exist |

The reason real data never descends is twofold: privacy/regulatory exposure and blast radius. If a lower stage cannot contain customer data, no lab accident or test can ever leak it. Where production-realistic data is needed for validation, it is *manufactured* (synthetic generators, differential-privacy sampling of aggregates at most), never copied.

---

## 4. Feature Promotion Pipeline

The canonical path — **no shortcut allowed**:

```
Development → Internal Testing → Compliance Validation → Release Candidate
   → Production Release → Customer Rollout → Monitoring → Hotfix → Patch → Production
```

**Chosen model: artifact-centric, gated promotion (App Store Connect + Vercel + trunk-based CI/CD).** A change becomes an **immutable, content-addressed build image** at the end of Development. That same image — not a rebuild — is what advances; each transition is a **gate** that passes automatically (all required checks green) or is blocked. Precisely: the *image* is immutable and identical across stages, while **configuration and secrets are externalized and promoted separately** (per-stage, per-region), and **database migrations are executed in each stage** (validated in Compliance first). So the guarantee is "the validated image is the shipped image, with only reviewed, promoted config differing" — not that every environment is byte-identical including data and config. Rollback stays trivial: re-point to a prior signed image (and run the paired down-migration if one was applied).

Each promotion writes an immutable pipeline record: artifact digest, source SHA, who/what promoted, which gate results, and the resulting stage. This is the supply-chain provenance trail (§13).

Two distinct axes must not be conflated:
- **Lifecycle stage** of a *feature* (`development → internal_testing → staging → pilot → production → deprecated → removed`) — already modeled on the feature flag.
- **Release channel** of an *artifact* (dev build → RC → signed production release). A feature can be `production` lifecycle-wise but still dark (flag off) in a given artifact. Keeping these orthogonal is what lets you "enable a completed feature without deploying code."

---

## 5. Multi-tenant architecture (the highest-stakes decision)

Requirement: thousands of customers, hundreds of ERP products, multiple regions, shared *and* dedicated databases, with migration between them.

**Chosen model: a *tiered* tenancy default driven by data sensitivity, not a single blanket model — over the AWS SaaS "pool / bridge / silo" taxonomy, routed by a control-plane tenant catalog.** This is a deliberate refinement over the generic "pool everything" SaaS default, because PFS tenants hold an **accounting ledger of record**, where per-tenant backup, point-in-time restore, and "export or erase exactly one tenant" (GDPR/audit) are hard requirements that are trivial with schema/DB isolation and painful with a shared pool.

- **Bridge = schema-per-tenant (default for ledger-of-record ERP tenants):** each accounting tenant gets its own Postgres **schema** within a shared cluster. This gives per-tenant migration control, per-tenant backup/restore/point-in-time recovery, and single-tenant export/erasure — the operations a financial system of record must support — while still sharing infrastructure. "Thousands" of tenants is well within reach of schema-per-tenant on modern Postgres with cluster sharding.
- **Pool = shared rows + RLS (default for the long tail and non-ledger workloads):** high-volume, low-sensitivity or free-tier tenants share tables with a `tenant_id` column and **Postgres Row-Level Security** enforcing isolation in the database below the application. Maximum density where per-tenant restore/erasure is not a ledger obligation.
- **Silo = dedicated database/cluster (on demand):** tenants with residency, contractual, performance, or heightened-regulatory needs get a fully dedicated database. Same application code; different connection target.
- **Tenant catalog:** a control-plane registry maps `tenant_id → {product, region, shard/schema/database, isolation_model, tier, active_release, license, module set, data_residency}`. Every request resolves its tenant *first*, then its data target. This indirection is the single source of truth for routing and is what makes **migration between isolation models** (bridge↔pool↔silo) a supported, online operation — provision target → dual-write/backfill → cut over the catalog pointer → verify → decommission source — rather than a rebuild.

**Defense-in-depth for isolation:** (1) the application always scopes by resolved tenant; (2) RLS policies (pool) or schema/database boundaries (bridge/silo) enforce it below the app even if the app forgets; (3) the tenant catalog + connection broker prevent cross-tenant connection reuse. A single-layer bug cannot leak data. **Cross-tenant leakage is a named, mandatory promotion gate** (§8) — RLS-bypass and schema-crossing are tested on every candidate, not assumed.

**Per-tenant resource governance (the pool failure mode):** pooled tenants get per-tenant connection limits (via a pooler such as PgBouncer), per-tenant rate/quota isolation at the API edge, and query-cost ceilings, so one noisy tenant cannot starve neighbors. Silo/bridge tenants are naturally isolated.

**Zero-downtime schema change:** all migrations follow the **expand/contract (parallel-change)** pattern — additive expand, backfill, switch, then contract in a later release — so a shared cluster is never taken down by a blocking DDL. Migrations are validated in Compliance against synthetic prod-shape data (forward *and* backward) before any tenant is touched, and roll out per shard.

**Regions:** the catalog is region-aware; data residency is a tenant attribute; releases can be rolled out per-region. Cross-region control-plane metadata replicates; tenant data does not leave its region.

---

## 6. Feature management plane

Already implemented (deny-by-default evaluation, five visibility levels, targeting, rollout, audit); this section states the target shape.

**Chosen interface: adopt the OpenFeature model** (vendor-neutral flag evaluation API) so the flag *provider* is an adapter. Today PFS is the provider; tomorrow the same call sites work if the provider changes. Evaluation stays **server-side and deny-by-default**, returning a decision + reason for audit.

Required targeting dimensions (superset of the spec): `hidden, experimental, internal, beta, early_access, region_specific, customer_specific, license_specific, version_specific, developer_only, platform_only, government_pending, deprecated, removed`. These are expressed as (visibility level × targeting rules × lifecycle stage × environment scope × version constraint × region constraint), evaluated deterministically. Rollout uses stable hash bucketing so a tenant's experience is consistent across calls.

**The invariant that matters:** enabling a finished feature is a *config* change (flag flip), never a deploy. The artifact ships dark; PFS lights it up per ring/region/customer/license.

---

## 7. Developer Workspace (owner-only, modular)

A registry-driven set of tools, each a module behind a `DevTool` port so tools are added without touching the shell. Target catalog (superset of what's shipped): Feature Manager, Migration Manager, Release Manager, Environment Manager, Database Inspector (metadata-only in Prod; full in Lab), Performance Dashboard, API Explorer, Queue/Job Inspector, Scheduler, Logs, AI Playground, Developer Console, Configuration Manager, **Secrets Manager (read-references-only; never plaintext)**, Permission Inspector, Audit Explorer, Health Dashboard, Integration Manager, Webhook Inspector, Background Jobs.

Every tool is `require_owner`-gated server-side (already implemented) and every privileged action audited. Tool capability is itself governed by feature flags (`platform_only` / `developer_only`), so tools can be dark-shipped and staged like any feature.

### 7.1 Customer Workspace (what a tenant actually sees)

Defined positively, not merely by negation. A customer tenant's shell mounts **only**: their ERP's licensed modules and data; their own users, roles, and branches; their reports; their configuration/settings; their license, plan, and usage; their available updates and release notes; their support channel (including the ability to *grant or decline* a support/impersonation session); and their billing. Nothing else.

**Hard rule:** no capability tagged `platform_only`, `developer_only`, `internal`, `experimental`, `hidden`, or `government_pending` is ever mounted in the customer shell, and no Developer-Workspace `DevTool` or hidden platform module is reachable from a customer route. This is enforced by the feature-flag evaluator (which forces customers to Production and denies elevated visibility server-side) and by the tenant realm having no operator identity — not by UI hiding. A customer who guesses an internal URL receives HTTP 403/401 from the backend.

---

## 8. Compliance & Validation engine (Stage 2 internals)

**Chosen design: a Validation Orchestrator running versioned, jurisdiction-scoped Compliance Packs behind a `CompliancePack` port, plus deterministic accounting-invariant suites.**

- **Compliance Pack** = a versioned bundle of rules for a jurisdiction (`us`, `ca`, `eu`, `me`, …), scoped by *effective date* (tax law changes over time), exposing `evaluate(context) → findings`. Packs are data+rules, not hardcoded branches, so adding a country is adding a pack — no core change. Rules are expressed declaratively (decision tables / rule DSL) behind a `RuleEngine` adapter so the engine (e.g., a JSON-rules evaluator today, a heavier engine later) can be swapped.
- **Accounting invariants** are property-based and golden-master tests, not opinion: double-entry (∑debits = ∑credits per entry and per period), GL ↔ subledger reconciliation, trial-balance closure, inventory valuation correctness under FIFO / LIFO / Weighted-Average against known fixtures, financial-statement identities (Assets = Liabilities + Equity; cash-flow ties to balance deltas), and bank-reconciliation completeness.
- **Engineering gates:** API contract/regression (against recorded contracts), permission matrix tests, security scans (authz, injection, XSS/CSRF surface), performance budgets (latency/throughput SLOs), migration validation (forward+backward, expand/contract, on synthetic prod-shape data), data-consistency checks, and a **dedicated tenant-isolation suite** — RLS-bypass attempts, schema-crossing, and cross-tenant leakage — which is a *mandatory* gate because a tenancy leak is the highest-severity failure this platform can have.

**Promotion rule:** an artifact earns a Release Candidate only when every *applicable* suite is green for every jurisdiction/module it targets. Human approval can be *required* for regulated changes but can never *replace* the automated gate.

**Compliance-pack governance:** packs are money-affecting — a wrong rule can wrongly block or wrongly allow a taxable release — so a pack is itself a **signed, versioned, provenance-bearing artifact** (§13), authored and reviewed under separation-of-duties, effective-dated, and independently rollback-able. A bad pack is reverted like any release, without touching engine code. Packs are validated (their own rule tests) before they can gate other artifacts.

Extensibility target: `USA, Canada, Europe, Middle East` packs first, arbitrary jurisdictions after, each with its own rule versions and effective-dating.

---

## 9. Intelligent release & deployment system

**Chosen model: ring-based progressive delivery with blue/green cutover and automated rollback (Microsoft rings + Google canary + SRE error budgets).**

- **Semantic Versioning** on immutable, signed artifacts; a **Release Registry** holds every release with provenance, changelog, and signature.
- **Rings / customer groups:** `internal → canary (1–5%) → early-access → broad → all`, optionally sliced by **region** and **license tier**. Rollout percentage and ring membership are flag-/catalog-driven.
- **Blue/Green** for the cutover (two prod colorways; switch traffic; instant switch-back) and **Canary** for statistical validation before widening.
- **Automatic rollback:** each release declares SLOs (error rate, latency, key business invariants like "journals still balance"). A canary/ring breaching its error budget triggers automatic rollback to the prior signed artifact — no human in the critical path.
- **Hotfix/patch lane:** an expedited pipeline that still passes Compliance (a reduced but mandatory suite) — fast, never unvalidated.
- **Deployment approval, release notes, change history** are first-class records on the Release Registry.

---

## 10. Security architecture (enterprise-grade)

- **AuthN:** operators and tenants are **separate identity realms** (already so). OIDC/SAML **SSO-ready**; **MFA-ready**; short-lived JWTs with **rotation** and asymmetric signing; **API keys** for machine/ERP callers with scoped capabilities.
- **AuthZ:** **RBAC** for coarse roles + **ABAC** for fine, context-sensitive decisions (attributes: tenant, region, license, stage, data-classification). Deny-by-default; every internal namespace 403s non-owners.
- **Tenant isolation:** application scope + Postgres **RLS** + tenant-catalog connection brokering (§5).
- **Encryption:** **in transit** everywhere — TLS to clients and **mTLS between planes/stages/services** (each stage is a separate trust boundary, so cross-stage calls are mutually authenticated); **at rest** for databases, backups, and release artifacts, with keys held in the KMS and rotated. Field-level encryption for the most sensitive attributes (e.g., bank/routing references) on top of at-rest.
- **Release integrity & key custody:** **signed releases** with **SLSA-style provenance** (who built what from which source), verified at promotion *and* at deploy; a candidate that fails attestation cannot enter Production. Signing keys live in the KMS/HSM under **separation of duties** — the identity that builds an artifact cannot be the sole identity that signs it for Production, and release-signing keys are rotated and access-audited. This makes §13's supply-chain guarantees cryptographic, not procedural.
- **Secrets:** a **Secrets Broker** backed by a KMS/Vault; **per-stage isolation** (Dev/Compliance/Prod secrets are distinct and non-overlapping); the UI shows *references*, never plaintext; PFS never stores raw credentials or payment data (owner performs those in the provider).
- **Privileged-session recording (all owner power, not just impersonation):** every privileged owner session — impersonation, emergency data repair, direct SQL maintenance, license/feature override, tenant migration, emergency unlock — is **session-recorded** with actor, before/after, and reason, in addition to the audit log. Impersonation additionally requires it be short-lived, capability-scoped, revocable, and never use a customer password (already a primitive: signed support sessions), and — where feasible — customer consent.
- **Web/platform hardening:** rate limiting (per-tenant and global), CSRF, XSS, SQL-injection protection (parameterized access + RLS), full immutable audit history, and the tenant-isolation test gate (§8).

Nothing is designed for hobby scale: every control above is a standard enterprise-SaaS expectation, and each is enforced server-side.

### 10.1 Hidden platform modules (owner-only, never in a customer ERP)

These exist only in the Control Plane / Developer Workspace, are `require_owner`-gated, feature-flagged `platform_only`, and every invocation is session-recorded with before/after + reason. None is reachable from any customer route:

Emergency data repair · Direct SQL maintenance (Lab-full; Prod runs only reviewed, parameterized, audited statements — never ad-hoc `SELECT *` of tenant rows) · Tenant migration (pool↔bridge↔silo, §5) · License override · Feature override · Remote diagnostics · Remote support session (distinct from impersonation: read-oriented diagnostics with the customer's consent) · Impersonation (fully audited, consented, session-recorded) · Rollback · Emergency hotfix/patch (expedited lane, still passes a mandatory reduced Compliance suite, §9) · Emergency unlock · Background repair job · Data-consistency repair (re-run invariant checks and apply reviewed corrections).

Because these mutate customer state, the highest-impact ones (repair, override, migration, direct SQL) require a **second-operator approval** (separation of duties) in Production and emit a high-severity audit + alert.

---

## 11. AI orchestration layer (agent-ready without redesign)

**Chosen design: AI agents are first-class but *ordinary* consumers of the same governed APIs — never privileged backdoors.** An **AI Orchestration** service exposes platform capabilities to models through a stable **tool contract (MCP-style)**: agents call the very same feature-flag-gated, RBAC/ABAC-checked, tenant-scoped, audited endpoints a human or ERP would. This is the decision that lets you add agents later "without redesign": there is no separate, weaker path for AI.

Components: a `ModelProvider` port (swap Anthropic/other providers as adapters); a **tool registry** (accounting, inventory, payroll, tax, compliance, forecasting, fraud detection, cash-flow, financial-health, support, developer-assist, DB-optimization, BI); **guardrails** (input/output policy, PII boundaries, per-tenant data scoping so an agent can never cross tenants); **human-in-the-loop** approval for state-changing actions; and an **evaluation harness** (agent outputs are validated like any other change — e.g., an AI-proposed journal still passes double-entry in Compliance). Agents propose; the platform's existing gates dispose.

Data boundary: agents operate on **read models** and sanctioned actions, tenant-scoped, region-respecting. No agent gets raw cross-tenant access.

**Cost & abuse governance:** per-tenant and per-agent **token/cost budgets** and rate limits (an agent cannot run the bill up or be used as a denial-of-wallet vector); **prompt-injection and tool-abuse defenses** — untrusted content (customer documents, web data) is never allowed to escalate tool permissions; tool calls are authorized by the *caller's* RBAC/ABAC context, not by anything the model was told; mutating tools require human-in-the-loop; and all agent tool-calls are audited exactly like human/API calls. Because agents ride the governed APIs, the tenant-isolation, feature-flag, and compliance gates already constrain them — the AI layer adds no new privileged path.

---

## 12. Extensibility & event-driven backbone

**Chosen backbone: event-driven with the transactional Outbox pattern over a broker, plus ports-and-adapters everywhere.**

- **Events:** state changes emit domain events (release promoted, tenant provisioned, flag changed, compliance failed, deployment rolled back). Producers use the **transactional outbox pattern** (write event + state in one transaction; relay to the broker) which gives **at-least-once delivery with idempotent consumers** — no dual-write bug, no lost events. Consumers are independent and replaceable.
- **Marketplaces:** ERP products and plugins self-register through a manifest against stable extension points — enabling an **ERP Marketplace** and **Plugin Marketplace** without core edits. (PFS already treats ERP products as self-registering.)
- **Adapters for the outside world:** country modules, tax engines, payment providers, bank integrations, and AI providers are all adapters behind ports. Adding one is adding an adapter + registering it; the core never changes.
- **No hardcoded workflows:** promotion gates, compliance packs, rollout strategies, and notification channels are configured/registered, not coded into the core path.

---

## 13. Supply chain, provenance & change integrity

Every artifact carries: source commit, build environment attestation, SBOM, digest, and signature. Promotion verifies the chain; deploy re-verifies the signature. This gives tamper-evidence ("what's running in Prod provably came from this reviewed source") and is the backbone of trustworthy rollback and incident forensics. It also satisfies the "only signed releases reach customers" invariant with cryptographic force rather than process trust.

---

## 14. Observability & SRE

Structured logs, metrics, and distributed tracing with **tenant/stage/release context** on every record (PFS already carries request/company/user context). Per-release **SLOs and error budgets** drive automatic rollback (§9). A Health Dashboard aggregates fleet + per-tenant health. Golden business signals (e.g., "period still balances", "tax totals within tolerance") are monitored as first-class SLOs, not just infra metrics — because in an accounting platform, correctness *is* availability.

---

## 14a. Data classification, privacy & disaster recovery

**Data classification** is the taxonomy the ABAC engine (§10) keys on and the DLP controls enforce: `public → internal → confidential → restricted (PII/financial ledger) → secret (credentials/keys)`. Every table/field is tagged; the tag drives who may read it (ABAC), whether it may be logged, whether it may appear in a read model an agent can see, and whether it may ever be exported.

**Privacy / DLP:** the "no real data descends" invariant (§3) is enforced, not assumed — there is **no credential path** from Production data to lower stages, and an egress/DLP control blocks copying `restricted`/`secret` data out of Production. Where lower stages need realism, data is *manufactured* by synthetic generators; at most, **aggregate**, non-re-identifiable statistics inform the generator's distributions (never row values). GDPR-style **single-tenant export and erasure** is a first-class operation — trivial under the bridge/silo defaults (§5), and supported under pool via tenant-scoped export/delete.

**Backup & disaster recovery** (absent from a first draft; mandatory for a financial platform): per-stage backup policy with **encrypted backups**; Production runs **continuous backup + point-in-time recovery** per tenant (schema/DB isolation makes single-tenant restore routine); **cross-region DR** for Production tenant data respecting residency; declared **RPO/RTO targets** per tier (e.g., restricted-tier tenants get the tightest RPO); and **regular restore drills** — a backup that has never been restored is not a backup. Release artifacts and compliance packs are themselves backed up and reproducible from provenance.

## 15. Mapping to the current PFS Control Center

This blueprint is reachable from today's code without a rewrite. What already exists and which target component it realizes:

| Target component | Already shipped in PFS | Gap to close |
|---|---|---|
| Control/Data plane split | Yes — metadata-only Control Center; ERPs own data | Formalize `TenantResolver`/catalog as a service |
| Product registry | Yes — `ErpProduct`, self-registration principle | Add marketplace manifest |
| Release registry | Partial — `Release`, imported-legacy, SemVer field | Add signing + SLSA provenance + channels |
| Environments | Partial — `MasterEnvironment` (dev/test/prod defined) | Elevate to isolated stage runtimes + reset/seed |
| Feature-flag plane | Yes — deny-by-default eval, visibility levels, targeting, rollout, audit | Adopt OpenFeature interface; add region/version dimensions |
| Developer Workspace | Yes — Developer Mode, owner-gated tools, preview sessions | Add Migration/Queue/Secrets/Config/Perf tools |
| Compliance stage | **Not yet** | Build Validation Orchestrator + Compliance Packs |
| Promotion pipeline | Partial — lifecycle stages on flags | Add artifact-centric gates + RC/signed channels |
| Multi-tenancy | Partial — `company_id` scoping, tenant context, isolation tests | Add tenant catalog + RLS/pool + schema-per-tenant bridge + online model migration |
| Impersonation | Yes — signed, scoped, revocable support sessions | Add session recording for the impersonation lane |
| Security | Yes — separate realms, RBAC, audit, additive migrations | Add ABAC, secrets broker, MFA/SSO, signing |
| AI layer | **Not yet** | Add orchestration service as a governed API consumer |
| Event backbone | **Not yet** (audit log exists) | Add outbox + broker |

## 16. Phased roadmap (recommended sequencing)

Ordered by dependency and risk-reduction, each phase shippable and reversible:

1. **Tenant catalog + isolation** (foundational). Introduce the catalog service behind the existing `company_id` scoping; implement pool+RLS and the schema-per-tenant (bridge) path; prove isolation with the dedicated leak-test gate; then add the silo escape hatch and online bridge↔pool↔silo migration.
2. **Artifact-centric release registry + signing.** Make releases immutable, signed, provenance-bearing, channel-aware (dev/RC/prod). Wire rollback to prior artifacts.
3. **Stage isolation.** Stand up Dev-Lab and Compliance runtimes per product with separate DBs/secrets; add lab reset + synthetic data factories; enforce the no-data-descends rule.
4. **Compliance & Validation engine.** Validation Orchestrator + first Compliance Pack (US) + accounting-invariant suites; make it a mandatory promotion gate.
5. **Progressive delivery.** Rings, canary, blue/green, SLO-driven auto-rollback; per-region/per-license rollout.
6. **Security hardening.** ABAC, secrets broker, MFA/SSO, session recording, SLSA verification at deploy.
7. **Event backbone + marketplaces.** Outbox + broker; ERP/plugin manifests.
8. **AI orchestration.** Tool registry + model-provider adapter + guardrails + eval harness, all over the governed APIs.

Feature flags gate every phase so half-built capability ships dark and is lit per ring — the platform never blocks on a big-bang cutover.

---

## 17. Deployment topology note (monolith-first, extract later)

The Control Plane is presented as many services, but the recommended *starting* topology is a **modular monolith** with hard internal module boundaries (the ports of §2), deployed once per stage, extracting services (flag evaluator, compliance orchestrator, AI orchestration) only when scale or team boundaries demand it. This is the industry-recommended path for a platform at PFS's current size: you get the clean seams now and pay distributed-systems cost only when it buys you something. The ports make later extraction mechanical rather than a rewrite.

---

## 18. Key architecture decisions (ADR summary)

| # | Decision | Chosen | Rejected alternative | Why |
|---|---|---|---|---|
| A1 | Stage isolation | 3 physically isolated stages, one-way promotion | Shared DB with "env" column | Blast radius + privacy; real data can never descend |
| A2 | Promotion | Immutable signed image + externalized promoted config, through gates | Rebuild per environment | "Validate the image you ship"; trivial rollback; provenance |
| A3 | Tenancy | **Tiered by sensitivity:** schema-per-tenant (bridge) default for ledger-of-record tenants, pool+RLS for long tail, silo on demand — catalog-routed | Blanket pool+RLS for everyone; blanket DB-per-tenant | Ledger of record needs per-tenant backup/restore/erasure; tiering spans the whole range with one codebase |
| A4 | Isolation enforcement | App scope **and** Postgres RLS **and** catalog broker | App-layer scoping only | Defense-in-depth; single-layer bug can't leak |
| A5 | Feature flags | Server-side deny-by-default, OpenFeature interface | Client-evaluated / hardcoded gates | No-deploy exposure; vendor-neutral; auditable |
| A6 | Compliance | First-class stage; versioned jurisdiction packs behind a port | Manual review; hardcoded per-country logic | Automated, effective-dated, extensible to any country |
| A7 | Releases | Rings + canary + blue/green + SLO auto-rollback | Manual all-at-once deploys | Progressive, self-healing, region/license-aware |
| A8 | AI | Agents consume the same governed APIs | Dedicated privileged AI path | Add agents with no redesign; no weaker security path |
| A9 | Backbone | Event-driven + outbox + ports/adapters | Synchronous, hardcoded workflows | Marketplace/provider extensibility; no core edits |
| A10 | Topology | Modular monolith first, extract on demand | Microservices from day one | Clean seams now; distributed cost only when justified |
| A11 | Secrets | Per-stage broker over KMS/Vault, references-only in UI | Secrets in config/DB | Enterprise secret hygiene; no plaintext exposure |
| A12 | Supply chain | Signed artifacts + SLSA provenance verified at deploy; signing under separation of duties | Process-trust ("we promise it's built right") | Cryptographic tamper-evidence; forensics; trustworthy rollback |
| A13 | Migrations | Expand/contract (parallel-change), per-shard, validated in Compliance | Blocking DDL on shared DB | Zero-downtime at thousands of tenants; reversible |
| A14 | Privileged sessions | Record + second-operator approval for high-impact owner actions | Audit log only | Insider-risk control; forensics; SoD on customer-mutating power |
| A15 | Data protection | Classification-driven ABAC + DLP; encryption at rest/in transit (mTLS); per-tenant PITR + cross-region DR | Perimeter security only | Financial-grade privacy, residency, recoverability |
| A16 | Compliance packs | Signed, versioned, effective-dated artifacts behind a port | Hardcoded per-country logic | Extensible to any jurisdiction; rollback-able; money-safe |

---

## 19. Risks & explicit tradeoffs

- **Tiered tenancy** is more operational surface than one blanket model: schema-per-tenant (bridge) means per-tenant migration fan-out, and pool+RLS adds query-time policy overhead. The payoff — per-tenant backup/restore/erasure for ledger tenants *and* density for the long tail — is worth it for an accounting system of record, and the tenant catalog keeps it one codebase. The dedicated leak-test gate (§8) is the mandatory mitigation for RLS risk.
- **Three isolated stages** cost more infrastructure and a real synthetic-data investment; this is the price of the privacy and blast-radius invariants and is non-negotiable for a financial platform.
- **Artifact-centric pipeline** front-loads build/signing complexity; it repays immediately in rollback safety and provenance.
- **Compliance packs** require ongoing legal/domain maintenance (tax law changes); effective-dated versioning is designed for exactly this, but it is a standing operational commitment, not a one-time build.
- **Modular-monolith-first** risks a module boundary eroding under deadline pressure; the ports and the decoupling guard tests already in the repo are the mitigation and must be kept enforced.

---

## 20. Closing principle

Every fork above was resolved toward **scalability, maintainability, and security over expedience**, matching how a billion-dollar SaaS platform (QuickBooks Accountant + Atlassian + GitHub + App Store Connect + Vercel + Stripe) is actually built. The design is intentionally incremental on top of what PFS already runs, so the strongest architecture is also a reachable one — no rewrite, no big bang, every phase shippable, reversible, and dark-launchable behind a flag.
