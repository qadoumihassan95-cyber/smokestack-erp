# PFS — The ERP Operating System: Master Platform Architecture

**Document type:** Master architecture / product-platform design (Chief Architect)
**Scope:** The whole of PFS as the operating system that owns ERP products, at tens-of-thousands-of-customers scale, multi-country, multi-agent.
**Relationship to prior docs:** Extends and supersedes the breadth of `ARCHITECTURE_THREE_STAGE.md` (which remains the authoritative deep-dive on the three stages, promotion gates, tenancy, and compliance). This document adds the platform module system, cell-based scale, the Claude→release supply chain, the feature marketplace, platform self-intelligence, and the AI agent runtime — and challenges the brief where a stronger enterprise pattern exists.
**Design bias (unchanged):** scalability, reliability, maintainability, security, extensibility, and developer experience over speed or simplicity.

---

## 0. Executive summary

PFS is an **ERP Operating System**: a control plane that owns many ERP *products*, each running for many *customers*, across three isolated *stages*, in multiple *countries*, governed by installable *compliance packs*, extended by a *marketplace* of modules, assisted by *AI agents*, and shipped through a *supply-chain-grade release pipeline*. Everything is managed centrally; nothing reaches a customer except a signed, validated release the owner has chosen to roll out.

The architecture is organized as **bounded-context modules behind stable ports** (hexagonal), deployed initially as a **modular monolith per stage** and sharded into **cells** as scale grows — the pattern NetSuite/Salesforce/Slack/AWS use to serve tens of thousands of tenants without a redesign. Tenancy is **tiered** (schema-per-tenant for ledgers of record, pool+RLS for the long tail, dedicated silos on demand) and routed by a **tenant catalog**. The Claude-authored-change requirement is realized as a **governed change-intake supply chain** — Git as source of truth, AI treated as an *untrusted contributor*, provenance and attestation on every artifact, automatic movement through Development and Validation, and a human/policy-gated signing step before any customer sees it. The marketplace treats ERP capabilities as **installable, versioned, licensed packages** (distinct from runtime feature flags). A **Platform Intelligence** layer watches the fleet and surfaces problems — deterministically for correctness-critical signals (accounting), statistically/AI-assisted for operational ones — before customers notice.

Sections 2 and 21 state exactly where I diverge from the brief and why.

---

## 1. Framing: PFS is an operating system, not an application

An OS owns processes (ERP products), schedules them across hardware (stages/cells/regions), manages drivers (integrations/providers), enforces permissions (RBAC/ABAC/tenancy), ships updates (release pipeline), and exposes a syscall surface (platform APIs). PFS is exactly this for ERP software. The consequences that shape every decision:

- **The kernel is small and stable; capability lives in modules.** The platform core depends only on *ports*; ERP products, compliance packs, marketplace modules, integrations, and AI agents are *adapters* loaded against those ports. Adding any of them never edits the kernel.
- **Two planes.** Control Plane (PFS: govern, observe, orchestrate — metadata only) vs Data Plane (ERP runtimes: execute, hold customer data). PFS never stores customer transactional data.
- **Everything is an artifact with provenance.** Releases, compliance packs, marketplace modules, and even Claude-authored changes are immutable, versioned, signed, attestable units — not ad-hoc edits.

---

## 2. Assumptions I am challenging (and the architecture I chose instead)

You asked me to challenge, not to comply. The important divergences:

1. **"Claude-generated updates automatically become candidate releases."** Correct *intent*, dangerous *literal* implementation. AI-generated code is **untrusted input** and must clear the same or stricter supply-chain gates as any human contributor — never a privileged fast path. I keep the "one place to upload" experience but back it with **Git as the source of truth + CI + provenance**, treat Claude as a named, sandboxed *contributor identity*, and make the automation stop at "green through Development + Validation." **Signing for production is a human/policy-gated act with separation of duties** (§7). Auto-signing AI output would be the single largest risk in the platform.

2. **"Every feature behaves like an installable app" conflated with feature flags.** These are two different mechanisms and must not be merged. A **marketplace module/package** is a versioned unit of *code + migrations + entitlements + dependencies* installed per tenant; a **feature flag** is a *runtime toggle* that gates behavior within already-deployed code. Shipping code through flags, or gating installation through flags, produces an unmaintainable mess. I keep both, cleanly separated (§8, §9).

3. **"Emergency SQL / Repair Ledger."** Raw SQL mutation of an accounting ledger is unacceptable as a normal tool. Ledgers are **append-only**; corrections are **reversing/adjusting entries** that preserve double-entry, produced through the accounting engine's own invariants — never destructive UPDATE/DELETE. "Emergency SQL" exists only as an audited, second-operator **break-glass** capability in the Developer Center, and ledger repair specifically is routed through compensating-entry tooling, not free SQL (§12).

4. **"Everything reversible / time travel."** True and safe in the **Development Lab** (snapshots, event-sourced replay, resettable DBs). In **Production**, "reversible" means *append-only history + compensating actions + release rollback + PITR* — not editing the past. I make that distinction explicit so the word "reversible" doesn't smuggle in destructive editing of customer books.

5. **"PFS becomes intelligent and detects problems."** Yes — but **correctness-critical detection (accounting anomalies, double-entry breaks, tax miscalculations) must be deterministic rules**, not ML inference; AI is for *operational* signals (performance, noisy patterns) and for *summarizing/recommending*, never for silently deciding financial truth. AI proposes; deterministic gates and humans dispose (§14, §15).

6. **Scale to tens of thousands.** A single tiered cluster (the prior doc) is necessary but not sufficient at that scale. I add **cell-based architecture** — the whole stack is sharded into independent *cells*, each serving a bounded set of tenants — for blast-radius containment, independent deploys, and linear horizontal growth (§5). This is the decisive scale decision.

7. **"One platform manages everything."** Central *control* — yes. Central *single database or single deployable* — no. Central management is a **control plane over many independently-failing cells and runtimes**; a global single point would cap scale and reliability. Management is logical, not physical, centralization.

---

## 3. Platform module architecture

PFS is composed of bounded-context modules grouped into planes. Each module owns its data, exposes a port, and communicates via APIs and domain events — never by reaching into another module's store.

**Control & Governance plane**
ERP Registry · Customer/Tenant Registry · License Manager · Entitlement Service · Identity Center (operator + tenant realms) · Audit Center · Session Manager.

**Delivery plane**
Release Manager · Version Manager · Release Builder · Release Signer · Change-Intake Service (the Claude pipeline, §7) · Environment Manager · Migration Manager · Deployment Center · Rollout Manager · Feature Manager (flags).

**Assurance plane**
Compliance Manager (installable country packs) · Validation Orchestrator · Monitoring Center · Telemetry · Health Center · Platform Intelligence (AIOps, §15) · Disaster Recovery.

**Product & Extensibility plane**
Marketplace · Plugin/Module Manager · Integration Center · API Manager · Webhook Manager · Notification Center.

**Intelligence plane**
AI Center / Agent Runtime (§14) · AI Playground.

**Commerce & Support plane**
Billing Center · Analytics Center · Support Center.

**Cross-cutting**
Secrets Manager · Configuration Manager · Event Backbone (outbox + broker) · Storage/Cache services.

### 3.1 Module interaction map (control plane)

```
      Identity Center ──auth──▶ every module (RBAC/ABAC context)
            │
   ERP Registry ─┐        ┌─ Feature Manager ─┐
 Tenant Registry ─┼─ read ─┤  License/Entitlement├─ decisions ▶ Data-Plane runtimes
   License Mgr ──┘        └─ Compliance Manager ┘        ▲
            │                                            │ signed releases
  Change-Intake ─▶ Validation Orchestrator ─▶ Release Mgr ─▶ Deployment/Rollout
        ▲ (Claude/Git)          │                 │              │
        │                       ▼                 ▼              ▼
   Marketplace ◀── modules ── Audit Center ◀── every mutation   Monitoring/Telemetry
                                   ▲                                  │
                              Platform Intelligence ◀── signals ──────┘
                                   │ recommendations
                              Developer Center (owner) / Notification Center
```

Every mutation emits a domain event to the Event Backbone (transactional outbox → broker, at-least-once + idempotent consumers). Audit Center, Platform Intelligence, Analytics, and Notification are all event consumers, so adding a new observer never touches producers.

---

## 4. Domain decomposition (DDD) and the port catalog

Bounded contexts map to the modules above. The kernel defines these **ports**; everything else is an adapter:

`ErpProductProvider`, `TenantResolver`, `ReleaseStore`, `ArtifactSigner`, `FlagEvaluator`, `EntitlementResolver`, `CompliancePack`, `MigrationRunner`, `SecretProvider`, `EventBus`, `ModelProvider` (AI), `IntegrationAdapter`, `PaymentProvider`, `TaxEngine`, `BankConnector`, `MarketplaceModule`, `Detector` (platform intelligence).

This is the single most important structural decision: it is what lets country packs, tax engines, banks, AI providers, and ERP products be added as adapters without redesign, and what makes later service extraction mechanical.

---

## 5. Cell-based, multi-tenant scale architecture (the tens-of-thousands decision)

**Chosen model: cells (a.k.a. shards/pods) of the full stack, with tiered tenancy inside each cell, routed by a global tenant catalog.**

- A **cell** is a self-contained, independently-deployable instance of a stage's stack (app + databases + cache + queue) serving a bounded population of tenants (e.g., low thousands). Cells do not share databases. A cell failure, bad migration, or noisy tenant is contained to that cell — blast radius is bounded by design.
- **Tiered tenancy inside a cell** (from the prior doc): **schema-per-tenant (bridge)** default for ledger-of-record ERP tenants (per-tenant backup/PITR/export/erasure), **pool + Postgres RLS** for the long tail, **dedicated silo** for the largest/regulated. 
- The **Tenant Catalog** (global, replicated control-plane data) maps `tenant → {product, cell, region, isolation_model, schema/db, active_release, license, module set, residency}`. All routing resolves the tenant first, then the cell, then the data target. This indirection makes **tenant relocation** (rebalance across cells, or migrate between isolation models) an online operation.
- **Regions & residency:** cells are placed in regions; a tenant's data never leaves its region; releases roll out per region/cell.
- **Why cells over one big multi-tenant cluster:** at tens of thousands, a single cluster couples everyone's fate (one bad migration = global outage), caps vertical scale, and complicates residency. Cells give linear horizontal growth, independent deploys/canaries per cell, and natural regional placement. This is the standard large-SaaS answer (AWS cell-based architecture, Salesforce instances/PODs, Slack shards). Note the citation carefully: those exemplars validate *cells*, not the intra-cell tenancy model — Salesforce, for instance, is **pooled + metadata (org_id)**, not schema-per-tenant. PFS chooses schema-per-tenant for ledger tenants for the backup/restore/erasure reasons in §2/prior doc, accepting its real cost (**migration fan-out across many schemas per cell**), mitigated by per-cell orchestration that applies a migration to every schema in the cell as one controlled, monitored operation.

**Control-plane static stability (the SPOF concern).** The global control plane must never be on the synchronous hot path of a customer request. The Tenant Catalog is **cached at each cell's edge** (with TTL + change-event invalidation), so a cell keeps serving its tenants — reads and writes to their ERP data — **even while the control plane is unavailable**; only control-plane *operations* (onboarding, release assignment, rebalancing) pause. The control plane itself is HA and regionally replicated with its own DR. This is the AWS cell principle applied, not just cited: the data plane survives a control-plane outage.

**Cell relocation & cross-cell concerns.** Moving a schema-per-tenant ledger between cells is an online, consistency-preserving cutover: **logical replication / dual-write → verify parity → flip the catalog pointer → drain → decommission source**, with the ledger's append-only nature making reconciliation deterministic. Customers that span cells/regions (multi-entity consolidation, global analytics) are served by **read-model aggregation** in the Analytics Center — the transactional ledgers stay cell-local and residency-bound; only permitted, classified aggregates are combined for reporting.

```
Global Control Plane (Tenant Catalog · Release Registry · Identity · Billing · Marketplace)
        │ routes/assigns tenants, publishes signed releases, aggregates telemetry
   ┌────┴───────────────┬───────────────────┬───────────────────┐
 Cell US-1            Cell US-2            Cell EU-1           Cell ME-1  ...
 (app+db+cache+q)     (app+db+cache+q)     (residency: EU)     (residency: KSA/JO)
 tenants 1..N         tenants N+1..2N      ...                 ...
 per-cell canary/deploy · per-cell backup/DR · per-tenant schema/pool/silo inside
```

---

## 6. The three stages & promotion pipeline (summary; full detail in the three-stage doc)

Three physically isolated stages — **Development Lab** (owner-only engineering playground where everything is editable and reversible: build/modify accounting, inventory, payroll, tax, AI, integrations, workflows, UI, databases, APIs, permissions; with feature flags, developer mode, experiments, debugging, synthetic data, performance/migration/security tests, database snapshots, rollback, time-travel replay, data seeding, load/stress testing, profiling, and telemetry; **each ERP has its own independent dev environment** — SmokeStack Dev, Dairy Dev, Auto Parts Dev, …), **Validation & Compliance** (automated accounting + jurisdiction + engineering gates via installable packs), **Customer Production** (only signed, validated releases; per-customer version/module/feature/license). One-way promotion; **real customer data never descends**. The canonical pipeline — Development → Internal Testing → Compliance Validation → Release Candidate → Signed Release → Production → Customer Rollout → Monitoring → Hotfix → Patch → Production — is enforced with no bypass. Feature *lifecycle stage* (of a feature) and *release channel* (of an artifact) are orthogonal axes so a finished feature can ship dark and be lit by flag without a deploy.

---

## 7. Change-Intake & Release Supply Chain (the "upload from Claude" requirement, done safely)

**Requirement:** one place to submit every change built in Claude; it becomes a candidate release, enters Development, moves through Validation automatically, becomes an RC, then a Signed Release, then the owner chooses which customers receive it; never deploy straight to customers.

**Chosen architecture: a governed Change-Intake Service backed by version control and a supply-chain-secure pipeline, with AI treated as an untrusted contributor.**

```
Claude output ─▶ Change-Intake Service ─▶ Git (source of truth, branch per change)
                    │ attaches provenance: author=claude-agent, prompt/session id,
                    │ diff, SBOM, signatures
                    ▼
            CI build ─▶ immutable, content-addressed image + attestation (SLSA)
                    ▼
        DEVELOPMENT (auto-deploy to Dev cell) ─ runs unit/integration/synthetic-data tests
                    ▼ (auto, only if green)
        VALIDATION & COMPLIANCE (auto) ─ accounting invariants + jurisdiction packs
                    │                     + regression + security + migration + tenant-isolation
                    ▼ (auto, only if all green)
        MANDATORY HUMAN CODE REVIEW of the diff  ◀── required for every AI-authored change
                    ▼ (only on explicit human approval of the code, not just the outcome)
        RELEASE CANDIDATE (immutable, provenance-complete)
                    ▼ (HUMAN signing gate — two-person integrity; a human signer is always required)
        SIGNED RELEASE (signed in KMS/HSM; builder ≠ reviewer ≠ signer)
                    ▼ (owner selects cohorts)
        CUSTOMER ROLLOUT (rings/canary/region/license) ─▶ Monitoring ─▶ auto-rollback on SLO breach
```

Why this shape:
- **Git as source of truth, not a bespoke uploader.** Every change is a reviewable, revertible, diffable commit with history — the industry standard. The "one place" is the intake UI on top of Git, not a magic box.
- **AI is an untrusted contributor identity.** Claude-authored changes carry an explicit `author=claude-agent` provenance tag and are *quarantined* to the same gates as any external contributor — plus stricter static analysis (secret scanning, dependency/license/typosquat checks, dangerous-API detection) because generated code is higher-risk. No AI change is exempt from any gate. The intake service itself is hardened against prompt-injection-through-content, and the generated dependency chain is pinned and scanned (a poisoned transitive dependency is an attack vector, not just a bug).
- **A human must read the code, not just the result.** Automation moves a change through Development and Validation, but **a mandatory human code review of the diff is required before it can become a Release Candidate**. Validation proves behavior; review proves *intent and safety* of the logic. Skipping this — reviewing only the green checkmarks — would make "untrusted contributor" theater. Review, build, and signing are three distinct identities (`builder ≠ reviewer ≠ signer`).
- **Automation ends before RC; signing always needs a human.** Movement through Dev and Validation is automatic (the productivity win). Becoming an RC needs human code review; **production signing is a two-person-integrity act that always requires a human signer** — policy may add *further* constraints but can never remove the human. This is the line that prevents "AI silently shipped to customers."
- **Provenance end-to-end (SLSA).** Every artifact carries source commit, build attestation, SBOM, and signature; verified at promotion and again at deploy. This gives tamper-evidence and forensics, and makes rollback trustworthy.
- **Never direct to customers.** The only path to a customer runtime is a signed release the owner has assigned to a cohort via the Rollout Manager.
- **Rollback ≠ undo-a-migration.** Auto-rollback on SLO breach re-points to a prior signed *image*; it cannot un-drop a column. Therefore **all schema change follows expand/contract (parallel-change)**: additive-expand and backfill ship first, the switch ships next, the destructive contract ships in a *later* release only after the new code is proven. A failed rollout is recovered by rolling back the image and, if needed, a **forward-fix** — never by reversing a destructive migration. Migrations are validated forward *and* backward in Validation before any tenant is touched, and applied per-cell/per-shard.

This satisfies the requirement's *experience* ("I upload once; it flows") while refusing its *literal risk* (auto-shipping unreviewed, unsigned AI code).

---

## 8. Feature Marketplace (ERP capabilities as installable apps)

**Chosen architecture: a package registry of versioned, licensed, dependency-aware modules — separate from feature flags.**

- A **marketplace module** (Payroll, Accounting, Manufacturing, POS, Restaurant, E-Commerce, CRM, HR, Banking, Warehouse, Production, Forecasting, …) is a package with: a manifest (id, semver, dependencies, required compliance packs, entitlement/pricing, migrations, extension points it registers), signed like a release, published to the **Module Registry**.
- **Per-tenant lifecycle:** install / upgrade / rollback / uninstall are first-class, tenant-scoped operations with their own migrations, resolved through a **dependency graph** (a module declares what it needs; the resolver refuses incompatible or unlicensed installs). Uninstall is safe (data retained/exported per policy; never silent deletion).
- **Entitlement, not just presence:** the Entitlement Service (driven by License Manager + Billing) decides whether a tenant *may* run a module; the module being installed and the tenant being entitled are separate checks (deny-by-default).
- **Versioned independently:** each module has its own release train through the same three stages; a tenant can run Payroll v4 and Inventory v7 simultaneously — the Tenant Catalog records per-module versions.
- **Distinction from feature flags (critical):** the marketplace controls *what code/capability is installed and licensed*; feature flags control *runtime behavior within installed code* (dark-launch, rollout %, region/customer targeting). A capability is *installed* via the marketplace and *exposed/tuned* via flags. Conflating them is a documented anti-pattern (§2.2).
- **Marketplace for ERP products too:** the same registry mechanism lists whole ERP products (SmokeStack, Dairy, Auto Parts…) as self-registering products — enabling an **ERP marketplace** and a **plugin marketplace** on one substrate.
- **First- vs third-party trust.** First-party modules go through the §7 supply chain. When *external* developers publish, they clear a stricter path: static/dynamic security review + sandboxed execution (capability-scoped, no ambient DB/tenant access — modules reach data only through the same governed ports and entitlements), publisher identity verification, and PFS counter-signing before a module is installable. An untrusted module can never see another tenant's data or escalate; the tenant-isolation gates apply to modules exactly as to core.

### 8.1 Gating precedence (single, unambiguous order)

Four mechanisms can grant/deny a capability; their precedence is fixed and evaluated top-down, deny-by-default at every step:

1. **License** — does the tenant's plan include this product/module at all? (no → deny)
2. **Entitlement** — is it currently entitled (paid, not suspended/expired, within seat/branch limits)? (no → deny)
3. **Installed & compatible** — is the module installed at a version whose dependencies and required compliance packs are satisfied? (no → deny)
4. **Feature flag** — is the specific behavior enabled for this context (region/customer/rollout/lifecycle)? (no → hide)

License and Entitlement answer *may they have it*; Marketplace answers *is it present and coherent*; Flags answer *is it turned on here*. A capability is visible only when all four agree.

---

## 9. Feature-flag plane (runtime governance)

Server-side, deny-by-default evaluation (already implemented) exposing every required dimension: `hidden, internal, experimental, beta, early_access, license_based, region_based, country_based, customer_based, developer_only, platform_only, government_pending, deprecated, removed`. Evaluation returns decision + reason for audit; rollout uses deterministic bucketing; customers are forced to Production and denied elevated visibility server-side. Recommend adopting the **OpenFeature** interface so the flag provider is itself an adapter. Invariant: exposing a finished feature is a config change (flag flip), never a deploy.

---

## 10. Compliance & Validation (installable country packs)

Validation is a first-class *stage*, driven by the Validation Orchestrator running **installable, signed, effective-dated Compliance Packs** behind a `CompliancePack` port: USA, Canada, Saudi, Jordan, Europe, VAT, GST, and future jurisdictions — added as packs, never as core edits. Deterministic accounting invariants (double-entry, journal integrity, GL, trial balance, balance sheet, P&L, cash flow, inventory valuation FIFO/LIFO/Weighted-Average, bank reconciliation, financial statements) plus engineering gates (regression, security, permission, migration, performance, API, tenant-isolation) and business-rule/AI/flag validation. Nothing reaches production signing without the applicable packs green. Packs are governed artifacts (authored/reviewed under separation of duties, versioned, rollback-able) because they are money-affecting.

---

## 11. Developer Center (Platform-Owner only)

One of the strongest surfaces, built as a registry of owner-only tool modules (each `require_owner`-gated server-side, feature-flagged `platform_only`, every action audited and — for privileged ones — session-recorded): Feature Manager, Developer Console, Database Inspector (metadata-only in Prod; full in Lab), Migration Manager, SQL Explorer (break-glass; §12), Queue/Job Inspector, Logs, Scheduler, API Explorer, Webhook Inspector, Performance Dashboard, Profiling, AI Playground, Configuration Manager, Environment Manager, Secrets Manager (references only, never plaintext), Permission Inspector, Release Builder, Release Signer (separated-duty), Rollout Manager, Background Jobs, Cache Manager, Storage Inspector, System Diagnostics, Monitoring, Tracing, Error Explorer. Never reachable from any customer route.

---

## 12. Hidden Owner Mode (break-glass, fully audited)

A complete owner-only operations surface for incidents, invisible to customers, every action **session-recorded with actor/before/after/reason** and — for state-mutating ops in Production — **second-operator approval (separation of duties)** and a high-severity alert:

Emergency SQL (break-glass, parameterized, reviewed — never ad-hoc ledger edits) · Repair Database/Ledger/Inventory/Accounting (**via compensating entries and the engine's own invariants, preserving double-entry — never destructive SQL**) · Tenant Migration (cell/isolation moves) · License/Feature Override · Remote Diagnostics · Support Session / Remote Login / Remote Maintenance (consented, short-lived, capability-scoped, recorded) · Emergency Unlock/Patch/Force Update/Force Migration · Background Repair · Rollback · Emergency Export/Backup · Data Recovery · Health Repair · Cache Flush · Queue Repair.

The governing principle (my divergence, §2.3): **the ledger is append-only; "repair" means correction entries, not rewriting history.** This is **enforced in the database, not merely in policy**: ledger tables carry no UPDATE/DELETE grant for the application or for break-glass roles, are protected by triggers/constraints (append-only, WORM-style), and posted entries are **hash-chained** so any tampering is detectable. Consequently "Emergency SQL" is constrained to non-ledger and read paths; it *cannot* mutate posted ledger rows even under break-glass — ledger correction always flows through the compensating-entry tool that preserves double-entry. Policy states the intent; the schema makes it true.

---

## 13. Customer ERP workspace (positive definition)

A customer tenant sees **only**: Dashboard, Accounting, Inventory, Purchases, Sales, Customers, Suppliers, Employees, Payroll, Reports, Settings, Branches, Users, Support, and their License. No developer tool, hidden owner module, or `platform_only`/`developer_only`/`internal`/`experimental`/`hidden`/`government_pending` capability is ever mounted in the customer shell — enforced by the backend (the tenant realm has no operator identity; internal routes 401/403), not by UI hiding.

---

## 14. AI Platform — plug-and-play agent runtime

**Chosen architecture: an Agent Runtime where every agent is a governed, sandboxed adapter that consumes the same platform APIs a human/service would — no privileged AI path.**

- **Agent contract (MCP-style):** each agent (Accounting, Payroll, Tax, Inventory, Compliance, Fraud, Cash-Flow, Forecast, Developer, Support, BI, Database, Optimization) declares the tools/capabilities it needs; the runtime grants **capability tokens** scoped by tenant, region, data-classification, and RBAC/ABAC. Plug-and-play = registering a new agent adapter; the kernel is untouched.
- **`ModelProvider` port:** model vendors are adapters (swap/route/fallback). **Model routing is residency-aware:** `restricted`/PII/ledger data may only be sent to a provider (or region/deployment of a provider) that satisfies the tenant's data-residency and processing constraints; where no compliant provider exists, the agent degrades to on-region/on-prem models or declines — data classification (§19) gates model selection just as it gates storage.
- **Isolation & guardrails:** per-tenant data boundary (no cross-tenant access, ever); token/cost budgets and rate limits (no denial-of-wallet); prompt-injection defenses (untrusted content can never escalate tool permissions; authorization is the *caller's* context, not the prompt's); mutating actions require human-in-the-loop and pass the same gates as any change (an AI-proposed journal still must satisfy double-entry in Validation).
- **Deterministic boundary (my divergence, §2.5):** AI **assists and recommends**; it does not decide financial truth. Correctness-critical outputs are validated by deterministic engines/packs before they can affect books.
- **Eval harness:** agents are versioned and evaluated (accuracy, safety, regression) through the same pipeline as code — an agent update is a release.

---

## 15. Platform Intelligence — proactive detection (AIOps done responsibly)

**Requirement:** PFS proactively detects performance, accounting, database, security, query, migration, compliance, license, feature-conflict, and integration problems and recommends fixes before customers notice.

**Chosen architecture: a detect → diagnose → recommend → (gated) remediate loop over the telemetry/event stream, with a clear split between deterministic and statistical detectors.**

```
Telemetry + Events + Audit ──▶ Detectors ──▶ Findings ──▶ Diagnosis ──▶ Recommendation
   (metrics, traces, logs,       │  deterministic:                        │
    ledger signals, query         │   double-entry breaks, tax mismatch,   ▼
    plans, migration reports)     │   trial-balance drift, RLS anomalies   Owner review / Notification
                                  │  statistical/AI:                       │  (auto-remediate only for
                                  │   latency/error anomalies, slow         ▼   safe, reversible, pre-
                                  │   queries, usage spikes, drift      Auto-remediation (policy-gated)  approved playbooks)
```

- **Deterministic detectors for correctness-critical and rule-bound signals** (accounting anomalies, double-entry, tax, GL/trial-balance drift, tenant-isolation/RLS anomalies, migration-risk static checks, **license expiry / over-entitlement / seat overage**, **feature-and-module conflicts** — incompatible versions, unmet dependencies, mutually exclusive modules installed together — and **compliance drift**: a tenant running below a pack's current effective-dated version, or in a jurisdiction whose rules changed): these are rules with zero tolerance for false "all clear," because in a financial system, **correctness is availability**.
- **Statistical/ML + AI detectors for operational signals** (latency/error-rate anomalies, slow-query detection via plan analysis, capacity/usage drift, integration failure patterns): anomaly detection + AI summarization to cut noise and explain.
- **Recommendation, not silent action.** Findings become ranked recommendations routed to the owner (Notification/Developer Center). **Auto-remediation is allowed only for a curated set of safe, reversible, pre-approved playbooks** (e.g., flush a cache, scale a cell, pause a risky rollout, open a support ticket) — never autonomous ledger or schema mutation. This is the responsible AIOps posture (Cloudflare/AWS/Datadog-style): automate the safe and reversible; escalate the rest.
- **SLOs & error budgets** (including *business* SLOs like "period balances") drive both detection thresholds and the auto-rollback in the release pipeline.

---

## 16. Identity, licensing & billing

- **Two identity realms** (operators vs tenants), OIDC/SAML **SSO-ready**, **MFA-ready**, short-lived rotating asymmetric JWTs, scoped **API keys** for machine/ERP callers.
- **RBAC** for coarse roles + **ABAC** for context (tenant, region, license, stage, data-classification).
- **License Manager + Entitlement Service** are the source of truth for what a tenant may run (product, modules, features, compliance packs, AI agents, seats/branches), consumed by the marketplace, flags, and runtimes.
- **Billing Center** (metered where relevant: seats, modules, AI usage) is an adapter over a payment provider; PFS never stores raw card data.

---

## 17. Security architecture (enterprise-grade)

Deny-by-default everywhere; backend-enforced authority (never trust frontend/params/headers). **Encryption in transit** (TLS to clients, **mTLS between planes/stages/cells**) and **at rest** (DBs, backups, artifacts; field-level for the most sensitive), keys in KMS/HSM with rotation. **Signed releases + SLSA provenance** verified at promotion and deploy, signing under **separation of duties**. **Tenant isolation** as defense-in-depth (app scope + RLS/schema/DB + catalog broker), with a **mandatory cross-tenant leak test gate**. Secrets via a per-stage broker (references only). Rate limiting (per-tenant + global), CSRF/XSS/SQLi protections, privileged-session recording, and a **tamper-evident audit trail** — hash-chained (each record commits to the prior) and WORM-stored, so any deletion or edit of history is detectable, not merely discouraged. The same hash-chaining protects the ledger (§12) and the release provenance (§7). AI-specific: prompt-injection/tool-abuse defense (§14).

---

## 18. Extensibility & the event-driven backbone

Domain events over a transactional **outbox → broker** (at-least-once + idempotent consumers). Ports-and-adapters make every external system pluggable: **Integration Center / API Manager / Webhook Manager** expose and consume; country packs, tax engines, payment/bank providers, AI providers, and marketplace modules are adapters. No workflow, gate, rollout strategy, or notification channel is hardcoded in the kernel — all registered. This is what enables the ERP marketplace, plugin marketplace, government/country modules, and provider integrations to grow without redesign.

---

## 19. Data architecture & data-flow rules

Data classification (`public → internal → confidential → restricted (PII/ledger) → secret`) drives ABAC and DLP. **No real customer data descends** to Dev/Validation — enforced by absence of any credential path down plus egress/DLP controls; lower stages use synthetic generators (aggregate-informed at most). CQRS/read-models where read/write asymmetry warrants (e.g., analytics, agent inputs). Per-tenant backup + PITR (trivial under schema/silo), cross-region DR, declared RPO/RTO per tier, and **restore drills**. The **production ledger is append-only**; corrections are entries, not edits (§2.4/§12).

---

## 20. Deployment topology & evolution

Start each stage as a **modular monolith with hard module boundaries** (the ports of §4), deployed per cell. Extract services **only when scale/team boundaries demand it** — the Validation Orchestrator, Agent Runtime, and Change-Intake pipeline are the first extraction candidates (compute isolation, blast radius, independent scaling). Cells shard the whole stack horizontally (§5). This gives clean seams now and distributed-systems cost only when it buys reliability or scale — the recommended path for a platform at PFS's stage that must reach tens of thousands without redesign.

---

## 21. Architecture Decision Records (consolidated)

| # | Decision | Chosen | Rejected | Why |
|---|---|---|---|---|
| P1 | Platform shape | ERP-OS: modular kernel + adapter modules | Monolithic ERP with add-ons | Extensibility; add products/packs/agents without kernel edits |
| P2 | Scale unit | **Cell-based** sharding of the full stack | One global multi-tenant cluster | Blast-radius containment; linear growth; residency; independent deploys at tens of thousands |
| P3 | Tenancy inside a cell | Tiered: schema-per-tenant (ledger), pool+RLS (long tail), silo (on demand) | Blanket pool; blanket DB-per-tenant | Per-tenant backup/restore/erasure for books of record + density for the tail |
| P4 | Claude change intake | Git-backed, provenance/attestation, AI = untrusted contributor, auto through Dev+Validation, **mandatory human diff review before RC + always-human SoD signing** (builder ≠ reviewer ≠ signer) | Auto-sign AI output; review only the green checks | Prevents shipping unreviewed AI code; keeps the one-upload experience |
| P13 | Control-plane availability | Static stability: catalog cached at cell edge, data plane serves through control-plane outage | Control plane synchronous on request hot path | Removes global SPOF; AWS cell principle applied |
| P14 | Capability gating | Fixed precedence License → Entitlement → Install/compat → Flag, deny-by-default | Overlapping ad-hoc checks | Unambiguous, auditable authorization |
| P15 | Ledger + audit integrity | DB-enforced append-only + hash-chained/WORM for ledger, audit, and provenance | Policy-only "immutability" | Tamper-evidence; break-glass cannot rewrite history |
| P5 | Marketplace vs flags | Separate: installable versioned/licensed **modules** ≠ runtime **flags** | One mechanism for both | Maintainability; correct lifecycle + entitlement semantics |
| P6 | Ledger repair | Append-only + compensating entries via engine invariants | Emergency raw-SQL edits as normal tool | Financial integrity; auditability; no destructive history edits |
| P7 | Platform intelligence | Deterministic detectors for correctness, statistical/AI for ops; auto-remediate only safe/reversible playbooks | AI decides/auto-fixes everything | Correctness is non-negotiable; responsible AIOps |
| P8 | AI agents | Governed adapters on the same APIs; capability tokens; no privileged path | Dedicated privileged AI backdoor | Add agents with no redesign; no weaker security path |
| P9 | Backbone | Event-driven, transactional outbox, ports/adapters | Synchronous hardcoded workflows | Extensibility; marketplace/provider growth |
| P10 | Topology | Modular monolith per cell → selective extraction | Microservices from day one | Clean seams now; distributed cost only when justified |
| P11 | Supply chain | Signed artifacts + SLSA, verified at promote & deploy, SoD signing | Process trust | Tamper-evidence; trustworthy rollback; safe for AI-authored change |
| P12 | Compliance | Installable, signed, effective-dated country packs | Hardcoded per-country logic | Any jurisdiction without core change; money-safe, rollback-able |

(Extends A1–A16 in `ARCHITECTURE_THREE_STAGE.md`; P1–P12 are the platform-level additions.)

---

## 22. Implementation roadmap (phased, reversible, grounded in current code)

Ordered by dependency and risk reduction; each phase ships behind flags, dark-launchable, reversible. Current state: PFS Control Center already has the control/data-plane split, product/customer/license registries, deny-by-default feature flags, Developer Mode + owner tools, signed-support-session impersonation, additive migrations, tenant context + isolation tests.

1. **Change-Intake supply chain (P4/P11).** Git-backed intake + CI + provenance/attestation + Claude-contributor identity; auto-flow through Dev + Validation; separated-duty signing; Release Registry with channels. *This directly realizes your "upload once" requirement and is the highest-leverage next build.*
2. **Tenant Catalog + cell routing skeleton (P2/P3).** Introduce the catalog and cell abstraction over today's scoping; one cell to start; prove tenant→cell→isolation routing and online relocation; add the leak-test gate.
3. **Validation & Compliance engine + first packs (P12).** Validation Orchestrator + USA pack + accounting invariants as a mandatory gate.
4. **Marketplace + Entitlement (P5).** Module Registry, per-tenant install/upgrade/rollback, dependency resolver, entitlement wiring; publish the first ERP capability as a module.
5. **Progressive delivery per cell/region (release pipeline).** Rings, canary, blue/green, SLO auto-rollback.
6. **Platform Intelligence v1 (P7).** Deterministic accounting/isolation detectors + operational anomaly detection + recommendation routing; safe auto-remediation playbooks.
7. **Agent Runtime (P8).** Capability-token runtime + first agents (Support, Developer, Accounting-assist) over governed APIs; eval harness.
8. **Security & DR hardening (P11/§17/§19).** ABAC, secrets broker, mTLS between cells, cross-region DR, restore drills, session recording everywhere privileged.
9. **Service extraction as needed (P10).** Extract Validation, Agent Runtime, Change-Intake when scale/teams demand.

---

## 23. Reference diagram — system context

```
                         ┌────────────────────────────────────────────────┐
     Platform Owner ───▶ │  PFS CONTROL PLANE (global)                     │
     (Developer Center,  │  Identity · Tenant Catalog · Registries ·       │
      Owner Mode)        │  Release/Change-Intake · Marketplace ·          │
                         │  Compliance · Intelligence · AI Center · Billing│
                         └───────┬───────────────┬───────────────┬────────┘
        Claude ▶ Change-Intake ──┘   signed releases │   telemetry/events │
                                                     ▼                    ▲
                         ┌─────────── DATA PLANE: cells × stages ─────────┴───┐
                         │  Dev cells      Validation cells    Production cells │
                         │  (per ERP)      (auto gates)        (per region)     │
                         │   ERP runtimes · tiered tenants · per-cell DR        │
                         └──────────────────────────┬─────────────────────────┘
                                                    ▼
                                            Customers (ERP workspace only)
```

---

## 24. Risks & explicit tradeoffs

- **Cells add operational complexity** (fleet of stacks, per-cell deploys, a routing/rebalancing system). The payoff — blast-radius containment and linear scale to tens of thousands — is why every large SaaS accepts it; mitigated by automating cell provisioning and using the catalog for relocation.
- **Tiered tenancy** is more surface than one model; justified for a ledger of record and kept to one codebase by the catalog.
- **The Claude intake is a security frontier.** Treating AI as an untrusted contributor with mandatory gates and separated-duty signing is the mitigation; the risk of the *literal* auto-ship interpretation is exactly why P4 refuses it.
- **Platform Intelligence auto-remediation** must stay narrowly scoped to safe, reversible playbooks; scope creep here is the failure mode, controlled by keeping the deterministic/AI and auto/escalate boundaries explicit.
- **Marketplace dependency management** is genuinely hard (version/compat matrices); the resolver + per-module release trains + compliance-pack requirements are the standard mitigation, but it is a standing engineering investment.
- **Modular-monolith boundary erosion** under deadline pressure is the classic risk; the ports and the decoupling guard tests already in the repo must stay enforced.

---

## 25. Closing principle

Every fork was resolved toward the enterprise-standard, scalable, secure option — cells for scale, tiered tenancy for correctness and density, a supply-chain pipeline that treats AI output as untrusted, a marketplace that separates packaging from runtime toggles, ledgers that are corrected not edited, and intelligence that automates only what is safe and reversible. The design is an evolution of what PFS already runs, so the strongest architecture is also a reachable one: no rewrite, every phase shippable, reversible, and dark-launchable — capable of serving tens of thousands of ERP customers across many countries without a redesign.
