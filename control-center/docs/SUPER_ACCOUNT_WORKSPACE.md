# PFS Platform Owner Workspace — "Mission Control" Architecture

**Document type:** Product & platform architecture (Chief Architect). Extends the approved `PLATFORM_OS_ARCHITECTURE.md`, `ARCHITECTURE_THREE_STAGE.md`, and `PLATFORM_INTELLIGENCE_EXTENSIONS.md`. Adds the operator-facing command center that controls every ERP, customer, deployment, agent, pack, integration, release, environment, and platform service — as **one workspace**, not an admin panel.
**Quality bar:** the operator experience of AWS/Azure/GCP consoles + Stripe Dashboard + GitHub Enterprise + Datadog, specialized for an ERP operating system.
**Design bias (unchanged):** scalability, reliability, maintainability, security, extensibility, DX over speed or simplicity.

> Written, then adversarially reviewed, then revised. §17 records the challenges and the resulting changes. This is the post-revision blueprint.

---

## 0. Executive summary

The Platform Owner Workspace is **one workspace over many governed services** — a thin, real-time client of the platform API that renders read-optimized views and dispatches typed, audited, gated commands. It is organized as a **modular shell** (identity, universal search, command palette, notifications, navigation) hosting independently-deployable **Centers** (Mission Control, Customer 360, Deployments, AI, Compliance, Integrations, Marketplace, Financial, Security, Observability, Knowledge, Intelligence, Remote Ops, Super Admin) plus new load-bearing capabilities the brief omitted: **Operator IAM & Governance**, a **Bulk-Operations Safety Engine**, **Incident Command**, **Notification/On-call**, **Runbooks/Automation**, **FinOps**, **Data Governance/DSAR**, **Policy-as-code guardrails**, **Audit/Forensics**, **Trust/Status**, and **full API/CLI/IaC parity**.

The four reframes that make it enterprise-grade rather than a god-mode admin panel: **(1)** the "Super Account" is a **Platform Owner Organization** with least-privilege roles + ABAC + break-glass + SoD, not one omnipotent login; **(2)** every fleet-wide/bulk action is an **orchestrated, previewed, staged, abortable job**, never a synchronous button; **(3)** the **console is a client of the same API** operators can script — no privileged backdoor; **(4)** Mission Control is an **attention queue over the Decision Intelligence Center**, not a wall of numbers. Everything is observable, searchable, controllable, auditable — and nothing lets the owner touch a raw customer DB or server: only governed operations.

---

## 1. Assumptions I am challenging

1. **"One login, unlimited power (Super Account)."** *Rejected as a single identity.* A single omnipotent super-admin is the platform's biggest compromise and insider-risk surface, with no separation of duties. Replaced by a **Platform Owner Organization**: one *workspace*, many *least-privilege identities and roles* (Owner, Operator, Support Engineer, Compliance Officer, Billing Admin, Security Officer, Read-only Auditor), ABAC scoping (by ERP product, region, customer segment, environment), mandatory **break-glass** for god-tier actions, and **session recording**. "One place to control everything" is a UX promise, not an authorization model (§3).

2. **"Bulk enable / deploy / migrate / restore / suspend across all customers."** *The highest-risk capability in the platform.* Redesigned as a **Bulk-Operations Safety Engine**: scoped selection → **dry-run preview with blast-radius** → staged/canaried execution → auto-halt on error budget → SoD approval for destructive or large scope → full audit and one-click abort/rollback. Bulk ops are orchestrated jobs, not buttons (§4).

3. **"Build an observability center (CPU/RAM/traces/logs/metrics)."** *Don't rebuild Datadog/Grafana.* PFS standardizes on **OpenTelemetry** and a proven metrics/log/trace backend, and the workspace adds the layer those tools *can't*: **tenant-, ERP-, and business-SLO-contextualized** views ("does this customer's period still balance," "which tenants breach their plan's latency SLO") synthesized by the intelligence layer. Compose best-of-breed; differentiate on ERP/business context (§ Observability Center).

4. **"Mission Control dashboard shows what requires attention."** *Yes — but it must be a triage inbox, not a second brain.* The dashboard is a **view over the Decision Intelligence Center's** ranked, evidence-linked findings + health signals; it does not re-implement detection. Prioritized problems with evidence, impact, and gated one-click remediation (§5).

5. **"Never open customer databases/servers manually."** *Kept and hardened.* The workspace exposes only **governed operations** (the same platform APIs); there is no raw SQL/SSH path in Production, Database Inspector is metadata-only in Prod, and any operation touching tenant data runs in-region under consent + audit (Remote Ops, Super Admin).

6. **"The console is the product."** *Reframed.* The console is a **client of the API**. Every action it performs is a public, audited API/CLI/IaC operation, so operators can automate at fleet scale and the console never becomes a privileged bottleneck (AWS/Stripe principle; ADR W1).

---

## 2. Workspace platform architecture (the substrate under every Center)

**Console-as-API-client + BFF + modular shell.** A **Backend-for-Frontend (BFF)** aggregates platform-service APIs into view-shaped payloads for the console, enforces the operator's ABAC context on every call, and never holds privilege the API wouldn't grant a scripted caller. The UI is a **modular shell**: a shared frame (identity, global search, command palette, notification center, nav) composing **Centers as independently-deployable modules** (micro-frontends or a modular SPA) — so Centers ship on their own cadence and teams scale, mirroring the platform's modular-monolith-then-extract stance.

**Read models (CQRS).** Each Center reads from **purpose-built read models/projections** fed by the event backbone (outbox → broker), not by querying transactional services directly. This keeps the console fast at fleet scale and decoupled from write-path load; read models are rebuildable from the event log (no single point of data loss).

**Command model.** Every mutating action is a **typed, idempotent Command** (`{verb, target(s), params, justification, idempotency_key, expected_version}`) that flows through one pipeline: **authorize (ABAC) → policy/guardrail check → blast-radius classification → (preview) → (approval if required) → execute via the owning platform service → audit (hash-chained)**. Two properties make this safe (added after review):
- **The command pipeline is a correctness gate; the console display is not.** Read models may lag, so the console can show stale fleet state — but every command carries an `expected_version` (etag) for its target aggregate, and the owning service **re-validates the target's authoritative state at execution time (optimistic concurrency) and rejects on drift**. An operator acting on a stale "healthy/idle" projection cannot fat-finger a destructive action against a tenant whose real state changed — the write path catches it. Idempotency keys make retries and bulk-resume exactly-once.
- **Delegated identity, no ambient privilege.** The BFF propagates the **operator's own identity/scope downstream via token exchange (RFC 8693 on-behalf-of)** — it never calls services with a broad service principal. ABAC is therefore enforced by each owning service against the operator's real scope, not by BFF logic alone (a BFF bug can't escalate).

Single-target actions take an **advisory lease per (tenant, resource)** with visible **operator presence** ("Operator X has an active session here"); conflicting concurrent commands are rejected by optimistic concurrency. There is no action the UI can take that isn't a first-class, replayable, audited command available via API/CLI — with one scoped exception: **god-tier actions are API-triggerable but always human-gated (break-glass), never fully IaC-automatable** (§3), so "full IaC parity" applies to read + routine commands, not to god-tier.

**Real-time.** A **streaming gateway** (WebSocket/SSE) pushes live updates (deployment status, health changes, new findings, incident updates) to the shell; real-time by default, polling on connection loss. At fleet scale the fan-out is sharded by topic, and **each operator's live stream is filtered by a pre-computed ABAC subscription set derived from their scope** (the PDP is not re-run per event) — the set is recomputed on grant/revoke events and **revocation propagates to live sockets mid-connection** (an operator whose scope was just narrowed stops receiving that tenant's stream immediately). This is the streaming counterpart to search's query-time trim (§6); backpressure sheds low-severity updates first, never critical alerts.

**Shared shell services.** Identity/session (§3), Universal Search (§6), Command Palette (keyboard-first action launcher over the command model), Notification Center (§ missing-capabilities), Navigation/IA (§14).

```
        Operator ─▶ Modular Shell (search · palette · notifications · nav · identity)
                         │ view reads            │ typed commands
                         ▼                        ▼
                 BFF (ABAC-enforced, view aggregation)  ──▶ Command Pipeline
                         │                                   (authz→guardrail→blast-radius
                 CQRS read models  ◀── events ── platform    →preview→approval→execute→audit)
                 (per Center)                     services ──────────────┘
                         ▲                                   │
                 Event backbone (outbox→broker) ◀────────────┘   Streaming gateway ─▶ live UI
```

---

## 3. Operator IAM & Governance  *(NEW — CRITICAL; the "Super Account" done right)*

**Purpose.** Turn "one super login" into a governed operator organization with least privilege, SoD, and break-glass — the workspace's authorization foundation.

**Responsibilities.** Authenticate operators (separate realm from tenants); assign roles + ABAC scopes; enforce SoD and approvals; issue/rotate break-glass; record privileged sessions; manage delegation and time-boxed elevation.

**Architecture.** Operator identity realm (OIDC/SAML SSO, mandatory MFA, hardware-key for god-tier), **RBAC + ABAC** decision point in the command pipeline (§2). Roles: Owner, Operator, Support Engineer, Compliance Officer, Billing Admin, Security Officer, Release Manager, Read-only Auditor. ABAC attributes scope authority by ERP product, region/residency, customer segment, environment, and data classification. **Break-glass**: god-tier actions (kill switch, tenant transfer, ledger correction, license override) require just-in-time elevation with reason, second-operator approval, a short TTL, and session recording; elevation auto-expires.

**Domain model.** `Operator`, `Role`, `Grant{role, scope(ABAC)}`, `ElevationRequest`, `BreakGlassSession`, `ApprovalPolicy`, `DelegationGrant`.

**Services.** Auth; Authorization (policy decision point); Elevation/Break-glass; Approval workflow; Session recorder; Delegation.

**APIs.** `whoami/context`, `grant/revoke`, `requestElevation`, `approve`, `sessions`, `delegate`. **Events.** `OperatorGranted, ElevationRequested/Granted/Expired, BreakGlassOpened/Closed, ApprovalDecided`.

**Permissions/Security/Audit.** This *is* the permission system; itself deny-by-default; every grant/elevation/session hash-chained into the tamper-evident audit; no operator can grant themselves elevation (SoD enforced).

**Break-glass root of trust (the bootstrap problem — added after review).** SoD means no one self-approves elevation — including the Owner. Therefore: **(1)** the Owner role has **no unilateral god-tier path**; Owner break-glass requires an **M-of-N quorum** (e.g., 2 of the Security Officers, or an external quorum) — approval is decentralized so a single compromised or coerced identity cannot reach god-tier. **(2) Approver-unavailable (the 3am problem):** if no approver responds within a bounded window during a declared incident, elevation escalates to a **time-delayed auto-grant** (short delay, escalating alerts to the whole on-call pool) with the session recorded at maximum fidelity and **mandatory post-hoc multi-party review** — recoverability without a silent bypass. **(3) PDP-independent last resort:** because the PDP fails closed, a major PDP/identity outage would otherwise make the platform unrecoverable; so there is a **physically separate, offline break-glass** — an offline-signed emergency credential / hardware-quorum path that does not depend on the live PDP, is stored under dual control, and whose every use triggers a high-severity incident and full review. Fail-closed never means self-inflicted unrecoverability.

**Scalability/Failure/Recovery.** Authorization is cached per session with short TTL + revocation events; PDP outage → fail-closed (deny) for normal actions, with the offline last-resort path above for recovery; identity provider outage → existing sessions honored to TTL, new logins blocked. Grants replayable from the event log.

**UI/UX/Nav.** An "Organization & Access" Center: operators, roles, scopes, active elevations, pending approvals, session recordings. UX makes elevation deliberate (reason required, TTL shown, "you are in break-glass" persistent banner).

**Dependencies.** Underpins every other Center (all commands pass its PDP). **Roadmap:** (1) roles+ABAC+MFA+audit; (2) break-glass+approvals+session recording; (3) delegation+time-boxed elevation; (4) hardware-key/step-up for god-tier. **ADR W2:** Platform Owner Organization with least-privilege + SoD + break-glass replaces a single super-admin.

---

## 4. Change Management & Bulk-Operations Safety Engine  *(NEW — CRITICAL)*

**Purpose.** Make fleet-wide and bulk actions safe at tens-of-thousands scale — the difference between a platform and a loaded gun.

**Responsibilities.** Turn any bulk/fleet command into a governed, previewed, staged, abortable job with blast-radius controls, approvals, and rollback.

**Architecture.** A **Change Orchestrator** wraps the command pipeline for multi-target actions: **(1) Selection** — target a *saved segment* (query over the graph/read models: "all US restaurant-ERP tenants on v<7 with active license"), never "everything" by accident. **(2) Plan/Preview** — a dry-run that renders the exact per-target diff and a **blast-radius score** (how many tenants, what data, reversibility). **(3) Policy/guardrail check** — org policies (e.g., "no bulk op >N tenants or touching `restricted` data without SoD approval"). **(4) Staged execution** — even admin actions roll out in **rings/canary** (1% → 10% → 100%), rate-limited, respecting maintenance windows. **(5) Auto-halt** — on error-budget breach or anomaly, the job **pauses automatically** and alerts. **(6) Abort/rollback** — one action stops and, where reversible, reverses. **(7) Audit** — every target outcome recorded.

**Domain model.** `Segment`, `ChangePlan{targets, diff, blast_radius, reversibility}`, `ChangeJob{status, ring, progress}`, `Approval`, `HaltCondition`, `RollbackPlan`.

**Services.** Segment engine; Planner/Dry-run; Guardrail/Policy engine; Ring executor; Halt monitor; Rollback; Job history.

**APIs.** `planChange(command, segment)`, `approveChange`, `executeChange`, `pause/resume/abort`, `rollbackChange`, `jobs`. **Events.** `ChangePlanned, ChangeApproved, RingStarted, ChangeHalted, ChangeAborted, ChangeRolledBack, ChangeCompleted`.

**Permissions/Security/Audit.** Destructive or large-blast plans require SoD approval (§3); the whole job hash-chained; irreversible operations (bulk restore/suspend/delete) demand explicit typed confirmation of the blast radius and are never fast-pathed.

**Correctness at scale (added after review).** Four mechanisms make fleet action safe: **(1) Per-target idempotency keys** — every target application is exactly-once; a resumed or retried job never double-applies (resume-from-ring is safe). **(2) Conflict detection & leases** — at plan and at execute, the segment is intersected with in-flight jobs; overlapping targets are surfaced ("this segment intersects 342 tenants in active ChangeJob #918") and a **per-target lease** prevents two jobs mutating the same tenant concurrently. **(3) Cross-cell saga** — a fleet job that spans cells/regions runs as a **saga: a coordinator with per-cell sub-jobs**, each executing/halting/rolling back within its own cell and residency boundary; the coordinator aggregates progress and can halt globally, while error budgets are evaluated **per cell** (a bad cell halts itself without dragging healthy cells down). **(4) Dry-run re-validation** — the preview's blast radius is recomputed, and each target's live state is **re-validated at ring entry** (optimistic concurrency); targets that drifted since preview are skipped and flagged, so *blast radius shown = blast radius applied*.

**Scalability/Failure/Recovery.** Jobs are durable and resumable; a worker crash resumes from the last committed ring via idempotency keys; partial failure leaves a precise per-target ledger for targeted retry/rollback; **default posture is fail-safe (pause), not fail-fast (continue)**.

**UI/UX/Nav.** A "Change & Bulk Operations" Center: build a segment, preview the diff + blast radius, request approval, watch the ringed rollout with a live halt/abort control. UX foregrounds blast radius and reversibility; destructive actions use deliberate friction (type the count, name the reason).

**Dependencies.** Command pipeline (§2), Operator IAM (§3), Rollout Manager, Decision Center (risk), read models/graph (segments). **Roadmap:** (1) segments+dry-run+audit; (2) ringed execution+auto-halt; (3) guardrails+SoD approvals; (4) rollback+maintenance windows. **ADR W3:** all bulk/fleet actions are orchestrated, previewed, staged, abortable jobs — never synchronous.

---

## 5. Mission Control Dashboard  *(CRITICAL)*

**Purpose.** Immediately answer "what requires my attention?" by prioritizing problems, not displaying static numbers.

**Architecture.** Two tiers, so a Decision-Center outage can never look like "all clear" (corrected after review). **Tier 1 — ranking:** the **Decision Intelligence Center** supplies ranked, evidence-linked findings (unhealthy customers, failed deployments, blocked releases, outdated packs, failing agents, disconnected integrations, expiring subscriptions, abnormal resource use, new tax regs, risky flags, environments needing intervention, failed backups, churn risk), each with severity, evidence, impact, recommended action, and **gated one-click remediation** (through the command pipeline + guardrails). Mission Control renders and routes these; it does **not** compute them. **Tier 2 — raw critical fallback:** Mission Control **also subscribes directly to raw high-severity Health and Security event streams**, bypassing the Decision Center. If the Decision Center is degraded or down, critical alerts still surface (unranked), and the UI shows an explicit **"intelligence degraded"** state — never a false "no signals / all clear." Absence of ranking is distinguished from absence of problems.

**Domain model.** `AttentionItem{finding_ref, severity, evidence[], impact, recommended_action, status, owner}`, `Signal`, `Acknowledgement`, `Snooze`.

**Services.** Feed aggregator (subscribes to Decision Center + Health + Security + Deployment events); prioritizer (uses Decision Center ranking); ack/snooze/assign; SLA timers.

**APIs.** `attention(scope, filters)`, `acknowledge`, `assign`, `snooze`, `resolve`. **Events.** `AttentionRaised, Acknowledged, Resolved, Escalated`.

**UI/UX/Nav.** Triage inbox as the landing view: severity-sorted, filterable by domain/segment/region, each card expandable to evidence + action; a "situation" overview (fleet health heatmap by cell/region) below the queue. Keyboard-first (j/k navigate, e resolve). Personalized per role (a Billing Admin sees billing findings first).

**Permissions/Security/Audit/Scale/Failure.** Inherit workspace defaults (§13); attention items are permission-trimmed (§6); ack/resolve audited; feed degrades to periodic refresh if streaming drops; empty/stale intelligence → shows "no prioritized signals" honestly rather than fabricating. **Dependencies:** Decision Center, all signal sources. **Roadmap:** (1) queue over Decision Center; (2) one-click gated remediation; (3) per-role personalization + SLA timers; (4) fleet heatmap + situation view. **ADR W4:** dashboard is an attention queue over the intelligence layer, not a metrics wall.

---

## 6. Universal Search  *(CRITICAL)*

**Purpose.** Instantly find anything — customers, users, invoices, products, deployments, logs, errors, releases, modules, agents, knowledge, sessions, packs, API keys, audit — one search box.

**Architecture.** A dedicated **search platform** (OpenSearch/Elasticsearch-class) fed by an **indexing pipeline off the event backbone**; the **Knowledge Graph** powers relationship/semantic search ("customers similar to X," "what depends on release Y"). Two critical properties: **(1) permission-trimmed at query time** — results are filtered by the operator's ABAC scope *server-side*; an operator never sees a result they can't access, and the mere existence of a record isn't leaked. **(2) Federated ranking** across entity types with type-ahead and scoped filters.

**Domain model.** `SearchDocument{type, id, tenant, fields, acl_tags, classification}`, `Index`, `Query`, `Result`.

**Services.** Indexer (event→document); Query service (ABAC-filtered); Suggest/type-ahead; Relationship search (graph-backed).

**APIs.** `search(q, filters, scope)`, `suggest(prefix)`, `related(entity)`. **Events.** `DocumentIndexed, DocumentRemoved`.

**Permissions/Security/Audit.** ABAC filter is non-negotiable and applied in the query layer, not the UI; searches over `restricted`/PII entities are audited; classification travels with each document. **Result counts and pagination are trimmed too** (not just the visible rows), and query latency is normalized, so existence can't leak via count or timing side-channels. To keep least-privilege from being undermined by pre-emptive over-scoping, search offers a **zero-knowledge escalation request**: an operator who legitimately needs a record outside their scope during an incident can request **scoped, approved, time-boxed access** without ever seeing the record until granted. **Scale:** sharded per cell/region; residency-respecting (a query never crosses region for tenant data). **Failure/Recovery:** index rebuildable from the event log; index lag tolerated (search is not a correctness gate); stale results flagged. **UI/UX:** omnipresent search + command palette; grouped results by type; keyboard-driven; deep-links into the owning Center. **Dependencies:** event backbone, graph, IAM. **Roadmap:** (1) core entity index + ABAC trim; (2) relationship/semantic search; (3) saved searches → segments (feeds §4). **ADR W5:** search results (and counts/timing) are permission-trimmed server-side; existence is never disclosed; escalation is zero-knowledge.

---

## 7. The Centers (consistent template; cross-cutting facets inherit §13)

Each Center is a **surface over an already-designed platform service** — a read model + a set of typed commands — so the spec below emphasizes *purpose, the operator surface, what it reads, what it commands, permissions, and the new/notable facets*. **Security, Audit, Scalability, Failure/Recovery, and the command pipeline inherit the workspace defaults in §2/§13 unless a delta is noted.**

**Customer 360 Center** *(CRITICAL).* *Purpose:* one screen for a customer's entire reality. *Reads (CQRS):* license, subscription, ERP products, branches, users, roles, environments, flags, marketplace modules, installed agents, integrations, storage/DB/API/AI usage, monthly cost, health score, security & compliance status, recent deployments, release version, support sessions, tickets, invoices/payments, audit + activity timelines, performance, logs, backups/snapshots, recommendations — composed from graph + billing + health + intelligence read models. *Commands:* open support session, change license, toggle flags (gated), install module/agent, trigger backup, view (metadata-only) inspector. *Permissions:* tenant-scoped ABAC; PII/financial fields classification-gated; residency-aware (tenant specifics rendered in-region). *UX:* tabbed 360 with a health/attention header; every panel deep-links to its Center. *Deps:* graph, billing, health, intelligence, remote ops. *Roadmap:* profile → timelines → embedded actions → recommendations.

**Global Customer Management** *(CRITICAL).* Individual or bulk (enable, deploy, migrate, notify, license, compliance, AI, integration, backup, restore, export, import, archive, suspend, billing) — **all routed through the Bulk-Operations Safety Engine (§4)**; this Center is its primary surface (segments, preview, staged rollout, abort). *Delta:* no direct bulk mutation exists outside §4.

**Remote Operations Center** *(CRITICAL).* Surface over Support Sessions + Hidden Owner Mode: secure consented **support session, temporary time-boxed access, session recording**, remote diagnostics, health scan, restart services/workers/queues, flush cache, rebuild search index, repair integrations/jobs/config, restore backup, clone environment, move customer, emergency maintenance. *Delta permissions:* every action consented (customer grant), recorded, SoD for destructive ops; touches tenant data → in-region only. *UX:* a "cockpit" per session with recording indicator and an action palette of governed operations (never raw shell/SQL).

**Deployment Center** *(CRITICAL).* View over Release/Rollout/Deployment services: Dev/Validation/Prod status, rollback, canary, blue/green, customer rollout, deployment queue/health/history/timeline/failures/metrics, release adoption, version distribution. *Commands:* promote (gated by pipeline), start/pause/abort rollout, rollback (to prior signed artifact). *UX:* pipeline board + rollout ring visualizer + version-distribution heatmap by cell/region.

**AI Control Center** *(HIGH).* View over the AI Runtime: enabled/disabled agents, model, provider, cost, latency, token usage, success rate, failures, **hallucination/abstention alerts**, prompt versions, knowledge sources, RAG status, inference logs, agent permissions/health, upgrades. *Commands:* enable/disable agent (per tenant/cohort via flags), set budgets/rate limits, pin model/prompt version, roll back agent version. *Delta:* cost/rate governance and residency-aware model routing surfaced here.

**Compliance Center** *(CRITICAL).* View over Compliance packs + Knowledge Center regulatory-change feed: countries, tax/payroll/accounting packs, government rules, effective/expiration dates, verification, review status, pending updates, upcoming regulations. *Commands:* install/upgrade/rollback pack (gated, cited), assign pack to tenant/jurisdiction, schedule effective-dated change. *Delta:* pack changes are money-affecting → SoD + citation evidence.

**Integration Center** *(HIGH).* View over the Integration service/adapters (Stripe, Square, Shopify, WooCommerce, Amazon, eBay, WhatsApp, Telegram, Twilio, Google, Microsoft, bank/shipping/payment/email/SMS/accounting APIs, POS/barcode/IoT): per-integration status, errors, last sync, usage, **credential health**, logs. *Commands:* connect/reconnect (owner enters credentials via password-manager flow, never plaintext to PFS), test, disable, rotate. *Delta:* credentials are references; never displayed.

**Marketplace Center** *(HIGH).* View over the Module Registry: modules (accounting, inventory, payroll, CRM, POS, manufacturing, warehouse, restaurant, HR, forecasting, analytics, agents, packs, connectors, reports, dashboards, workflows, templates) with install, version, dependencies, compatibility, license, pricing, revenue, usage, health. *Commands:* publish/curate, approve third-party module (review+sandbox+counter-sign), set entitlement/pricing. *Delta:* install/upgrade per tenant flows through §4 for bulk.

**Financial Control Center** *(HIGH).* View over Billing: revenue, MRR, ARR, growth, churn, renewals, invoices, payments, outstanding balance, revenue by ERP/country/customer/module, forecast. *Commands:* adjust plan (gated), issue credit, dunning actions. *Delta:* separate from **FinOps/Cost** (§8) which is *platform cost/margin*, not customer revenue.

**Security Center** *(CRITICAL).* View over security services: failed logins, new devices, suspicious activity, permission changes, API keys/tokens/secrets (references), certificates, encryption status, MFA/SSO status, threat detection, incident timeline. *Commands:* revoke key/session, force MFA, quarantine, open incident (→ §8 Incident Command). *Delta:* feeds Mission Control high-severity findings.

**Observability Center** *(HIGH).* **Not a rebuilt Datadog** — a tenant/ERP/business-SLO-contextualized layer over an OpenTelemetry pipeline + a proven TSDB/log/trace backend: CPU/RAM/disk/DB/queues/workers/cache/API/latency/errors/traffic/network/storage/containers/logs/tracing/metrics/alerts, **plus business SLOs** ("period balances," "tax within tolerance") and per-tenant cost/health. *Commands:* set SLO/alert, drill to trace, correlate to deployment/change. *Delta:* deep-links to the underlying observability tool for raw exploration; PFS owns the *decision-grade* views.

**Knowledge Center** *(HIGH).* Surface over the Knowledge Center service (already designed): architecture, docs, tax/government PDFs, training, developer/support docs, release notes, SOPs, knowledge graph, AI knowledge — with ingestion, versioning, approval, citation, effective-dating. *Commands:* upload, submit for review, approve, expire, link to pack. *Delta:* approval SoD; the Citation Gate lives here.

**Platform Intelligence** *(HIGH).* Surface over the Decision Intelligence Center: proactive recommendations (deploy X, rollback Y, update pack, upgrade license, enable agent, repair integration, optimize DB, archive inactive customer, schedule maintenance) each with evidence, risk, impact, priority, recommended action, reasoning, expected result. *Commands:* accept (→ gated command), dismiss (with reason), snooze. *Delta:* this is the same engine that powers Mission Control; the difference is browsing/managing vs triaging.

**Super Admin Tools** *(CRITICAL).* Surface over Hidden Owner Mode: emergency mode, maintenance mode, tenant transfer, DB migration, license/feature override, **kill switch**, global read-only mode, emergency rollback/backup/restore, clone customer/ERP, repair tenant/config/queue/integration/permissions, **repair ledger (compensating entries only)**, disaster recovery, system recovery. *Delta:* every item is god-tier → break-glass + SoD + session recording (§3); ledger repair is append-only compensating entries (never raw SQL); DB-level enforcement per the platform doc.

---

## 7a. Deep-dive Centers that cannot inherit the template (added after review)

Four Centers are stateful, write-heavy, regulatory, or god-tier-heterogeneous and are specified in full rather than as thin surfaces.

### Remote Operations Center *(Critical)*
*Purpose/Responsibilities:* governed, consented, recorded remote management of a tenant — no raw DB/SSH, ever.
*Architecture:* a stateful **session lifecycle** — `consent grant → session open (time-boxed) → recorded activity → expiry/forced-termination` — over Support Sessions + Hidden Owner Mode. *Domain model:* `SupportSession, ConsentGrant, TimeBox, Recording, ActionInSession`. *Services:* session manager, consent, recorder, action dispatcher (governed ops only), reaper. *Events:* `SessionRequested/Consented/Opened/ActionInvoked/Expired/Terminated, RecordingSealed`.
*Recording storage, residency & access (the review's biggest hole — now specified):* a recording is a rendering of tenant PII, so it is treated as tenant `restricted` data — stored **in the tenant's own region**, encrypted, tenant-attributed, retention-bounded, and **hash-chained**. **Viewing a recording is itself a privileged, ABAC-gated, audited action requiring SoD approval** for PII-bearing recordings. Recordings never pool into a global control-plane store; they live in the residency domain of the tenant they depict.
*Failure/Recovery:* if the **recording pipeline fails mid-session, the session fails closed (elevated access is terminated)** — no unrecorded privileged activity is permitted; if the operator disconnects with access still live, the time-box + heartbeat reaper force-terminates and seals the partial recording; single-target leases + presence prevent two engineers operating the same tenant blindly. *Permissions:* consent + recording + SoD for destructive ops; in-region only. *UI/UX:* a per-session cockpit with a persistent recording indicator, remaining-time, an action palette of governed operations, and a visible "another operator is here" presence banner. *Deps:* IAM, Support Sessions, Hidden Owner Mode, command pipeline. *Roadmap:* consented recorded sessions → governed action palette → in-region recording store + gated viewing → clone/move/restore with blast-radius preview.

### Incident Command *(Critical)*
*Purpose:* declare, coordinate, and resolve platform incidents — an ops center without this is a dashboard.
*Architecture:* a **collaborative, write-heavy workflow** with a single authoritative incident state and many concurrent responders. *Domain model:* `Incident{severity, status, commander, responders[], affected(cells/tenants/services)}, TimelineEntry, CommsUpdate, Postmortem, ActionItem`. *Services:* declare/upgrade/resolve, role assignment (incident commander, comms, ops), timeline (append-only), stakeholder comms (→ Trust/Status + Notification), blameless postmortem. *Events:* `IncidentDeclared/Upgraded/Mitigated/Resolved, ResponderJoined, CommsPublished, PostmortemFiled`. *Concurrency:* the timeline is append-only (no lost updates); role assignment uses a lease (one commander); linked change-jobs/deployments are attached for a single source of truth. *Permissions:* any operator can declare; command roles gated. *Audit:* full immutable incident record feeds the audit + postmortem. *Failure/Recovery:* incident state is durable and survives Center outages; declared incidents are the one thing that can *loosen* automation (e.g., enable the break-glass escalation path, §3). *UI/UX:* an incident room — live timeline, affected-scope map, attached signals/changes, comms composer, resolution + postmortem. *Deps:* Mission Control (raw signals), Notification/On-call, Trust/Status, IAM. *Roadmap:* declare+timeline → roles+comms → linked changes/deploys → automated postmortem scaffolding.

### Data Governance / DSAR / Legal Hold *(Critical)*
*Purpose:* satisfy GDPR/residency: single-tenant/subject export and **erasure**, consent, legal hold, and a data map — a regulatory necessity for a multi-country ledger platform.
*The core conflict and its resolution (ADR W10):* DSAR **erasure** collides head-on with the **append-only ledger and hash-chained audit** — you cannot delete a row inside a hash chain without breaking it. Resolution is **crypto-erasure (key-shredding)**: PII is stored **encrypted with a per-subject data key** (envelope encryption); erasure = **destroy the subject's key**, rendering the PII permanently unrecoverable while the ledger entries, audit records, and hash chains remain structurally intact and verifiable. Financial *facts* required by law (amounts, dates) are retained per statutory retention; *personal identifiers* are crypto-shredded. Legal hold **suspends** key destruction for named subjects until released.
*Domain model:* `DataSubject, DSARRequest{export|erase}, SubjectKey, LegalHold, DataMap, ConsentRecord, RetentionPolicy`. *Services:* request intake + identity verification, export builder (in-region), key-shredding erasure, legal-hold manager, data-map/lineage (over the graph), retention enforcement. *Events:* `DSARRequested, ExportProduced, SubjectKeyDestroyed, LegalHoldPlaced/Released, RetentionExpired`. *Permissions:* privacy-officer role; erasure requires SoD + verified subject identity; legal hold overrides erasure. *Security/Audit:* the *fact* of erasure is audited (immutably) even though the data is gone; exports are in-region, encrypted, time-limited. *Scalability:* per-subject keys scale via a KMS key hierarchy; data map derived from the graph. *Failure/Recovery:* key destruction is irreversible by design — guarded by hold checks + SoD + a confirmation window; export failures are retryable. *UI/UX:* a request queue (export/erase), subject data-map view, legal-hold registry, residency dashboard. *Deps:* KMS, graph (data map), ledger, audit, IAM. *Roadmap:* export + data map → crypto-erasure + legal hold → consent + residency dashboard → automated retention.

### Super Admin Tools — per-operation classification *(Critical)*
A blanket "break-glass + SoD" is insufficient because god-tier operations differ wildly in blast radius and reversibility. Each is classified and gated accordingly:

| Operation | Blast radius | Reversibility | Gate |
|---|---|---|---|
| Global read-only / Maintenance mode | Fleet/cell | Reversible | Break-glass + 1 approver; auto-expire |
| Kill switch (disable feature/agent fleet-wide) | Fleet | Reversible | Break-glass + 1 approver |
| Emergency rollback | Cohort/cell | Reversible (prior signed artifact) | Break-glass + Release Manager |
| Emergency backup | Tenant/cell | Additive | 1 approver |
| Emergency restore | Tenant | **Destructive to current state** | Break-glass + M-of-N + typed blast confirm + tenant consent |
| Tenant transfer / DB migration | Tenant | Reversible w/ effort | Break-glass + M-of-N + dry-run (Twin) |
| License / Feature override | Tenant/cohort | Reversible | Break-glass + 1 approver |
| Repair tenant/config/queue/integration/permissions | Tenant | Mostly reversible | Break-glass + 1 approver + recording |
| **Repair ledger** | Tenant | **Append-only: compensating entries only, never raw SQL/DELETE** | Break-glass + M-of-N + Compliance sign-off |
| Disaster/System recovery | Region/fleet | Varies | Break-glass quorum + Incident + last-resort path (§3) |

All are surfaced only under break-glass, session-recorded, and DB-enforced where relevant (append-only ledger).

## 8. Capabilities the brief omitted — added, with rationale & rank

| # | Capability | Rank | Why it must exist |
|---|---|---|---|
| M1 | **Operator IAM & Governance** (§3) | Critical | A single super-admin is uninsurable; SoD + least-privilege + break-glass are table stakes for a platform touching money |
| M2 | **Bulk-Operations Safety Engine** (§4) | Critical | Fleet-wide actions are the top outage/compromise risk; they need preview + staging + halt + approval |
| M3 | **API / CLI / IaC parity** | Critical | Ops at tens of thousands must be scriptable; console-as-client prevents a privileged bottleneck and enables automation/DR |
| M4 | **Incident Command** | Critical | Declare/track incidents, timeline, comms, roles, blameless postmortems — an ops center without incident management is a dashboard |
| M5 | **Notification / On-call / Escalation** | Critical | Findings must reach the right human via the right channel (PagerDuty/Slack/email) with escalation, not just sit on a screen |
| M6 | **Data Governance / DSAR / Legal Hold** | Critical | GDPR/residency: single-tenant export & erasure, consent, legal hold, data-map — regulatory necessity for multi-country |
| M7 | **Policy-as-code Guardrails** | High | Org-wide rules ("no bulk op >N without approval," "no prod change in freeze window") enforced in the command pipeline, versioned & tested |
| M8 | **Audit / Forensics / eDiscovery** | High | A first-class surface to explore the tamper-evident audit, reconstruct incidents, and answer regulators/auditors |
| M9 | **FinOps / Cost & Metering** | High | Per-tenant platform *cost* and margin (compute, AI spend, storage) — distinct from customer revenue; drives pricing & efficiency |
| M10 | **Runbooks / Automation / Playbooks** | High | Codified, safe, reusable remediations (the safe/reversible auto-remediation set) — turns tribal knowledge into governed automation |
| M11 | **Trust / Status / SLA** | High | Status page, planned-maintenance comms, per-customer SLA tracking & credits — operator-managed customer trust |
| M12 | **Customer Success / Lifecycle** | Medium | Onboarding/offboarding orchestration, health→CS motions, churn playbooks — turns health scores into action |
| M13 | **Quota & Rate-limit Management** | Medium | Per-tenant quotas/limits to prevent noisy-neighbor and abuse, tunable per plan |
| M14 | **Localization & Multi-currency** | Medium | Multi-country operators and customers need i18n, locale, currency, timezone throughout |
| M15 | **Mobile / On-call Companion** | Future | Critical alerts + acknowledge/abort from a phone for on-call; not full workspace |
| M16 | **Operator Productivity** (command palette, keyboard, saved views, bulk selection) | High | The difference between "usable" and "world-class"; power-operators live in the keyboard |
| M17 | **Sandbox / What-if** (preview a change's effect via the Digital Twin from the console) | Medium | Lets an operator simulate before acting, wired to the Twin |

Each inherits the workspace platform (§2), IAM (§3), and command/audit model; each is a Center or a shell service.

---

## 9. Navigation & Information Architecture

Top-level nav grouped by **operator intent**, not by internal service:

- **Operate** — Mission Control, Incidents, Deployments, Remote Ops, Change/Bulk Ops.
- **Customers** — Customer 360, Global Management, Financial, Customer Success, Trust/SLA.
- **Build & Extend** — Marketplace, Integrations, AI Control, Compliance, Knowledge, Developer Center (from platform doc).
- **Observe** — Observability, Platform Intelligence, Audit/Forensics, FinOps.
- **Govern** — Organization & Access (IAM), Policy/Guardrails, Data Governance/DSAR, Security, Super Admin.

**UX principles.** One workspace, progressive disclosure (fleet → segment → tenant → entity); **keyboard-first** (global command palette dispatches any command; j/k/e/g shortcuts); density toggles; **safety affordances** on destructive/bulk actions (blast-radius shown, type-to-confirm, reason required, persistent break-glass banner); real-time by default; deep-linkable everything; role-personalized landing; dark/light; accessible (WCAG AA); i18n/multi-currency. **Command palette** is the connective tissue — every Center action is reachable by search-then-run, and every run is an audited command.

---

## 10–13. Cross-cutting facets (stated once; Centers inherit)

**Permissions (§ all).** Every read and command passes the IAM PDP (§3); deny-by-default; ABAC-scoped; results permission-trimmed (§6). **Security.** mTLS to services; no plaintext secrets in the console; classification-aware rendering; console is a client of the API with no extra privilege; god-tier via break-glass. **Metadata-only preserved:** where the console renders tenant specifics (Customer 360 fields, a bulk dry-run diff, a recording), those are **transient, in-region projections streamed to the authorized operator — never persisted control-plane-side and never crossing region**, so the two-plane/no-data-descends invariant holds even as the owner "sees everything." **Audit.** Every command + privileged view is hash-chained into the tamper-evident audit; bulk jobs record per-target outcomes; session recording for privileged/remote/break-glass sessions. **Scalability.** CQRS read models per cell/region; BFF stateless and horizontally scaled; search/observability on their own backends; real-time via a scalable streaming gateway; the console degrades gracefully (read-only, polling) under partial outage. **Failure modes.** PDP down → deny; read-model lag → show freshness + stale badge (console is not a correctness gate); streaming down → poll; a Center's backend down → that Center degrades, the shell and others survive (fault isolation). **Recovery.** Read models/search/indexes rebuildable from the event log (RPO≈0 for derived state); commands replayable; RTO bounded by projection drain. **Performance targets.** Shell/nav interactive < 1 s; Center view p95 < 2 s; search suggest < 150 ms; command dispatch ack < 500 ms; real-time push < 2 s end-to-end.

---

## 14. Capability ranking (master)

**Critical (build first — the platform is unsafe/unoperable without them):** Operator IAM & Governance, Bulk-Operations Safety Engine, API/CLI/IaC parity, Mission Control, Universal Search, Customer 360, Global Customer Management, Remote Operations, Deployment Center, Compliance Center, Security Center, Super Admin Tools, Incident Command, Notification/On-call, Data Governance/DSAR.

**High:** AI Control, Integration, Marketplace, Financial, Observability, Knowledge, Platform Intelligence, Policy Guardrails, Audit/Forensics, FinOps, Runbooks/Automation, Trust/Status/SLA, Operator Productivity.

**Medium:** Customer Success/Lifecycle, Quota/Rate-limit, Localization/Multi-currency, Sandbox/What-if.

**Future:** Mobile/On-call companion; predictive fleet autopilot (advisory); marketplace revenue analytics deepening; cross-operator collaboration (shared investigations).

---

## 15. Consolidated ADRs

| # | Decision | Chosen | Rejected | Why |
|---|---|---|---|---|
| W1 | Console model | Console is a client of the platform API; API/CLI/IaC parity for read+routine (god-tier API-triggerable but human-gated, not IaC-automatable) | Console-only privileged admin; or "full parity" incl. god-tier | No backdoor; scriptable; DR-able — without letting a pipeline bypass break-glass |
| W2 | "Super Account" | Platform Owner Org: least-privilege roles + ABAC + SoD + break-glass | Single omnipotent super-admin | Removes single point of compromise; auditable insider control |
| W3 | Bulk/fleet actions | Orchestrated: segment→preview→stage→halt→approve→rollback | Synchronous bulk buttons | Bounded blast radius; safe at tens of thousands |
| W4 | Dashboard | Attention queue over the Decision Center | Static metrics wall | Prioritizes problems; single intelligence source |
| W5 | Search | Permission-trimmed server-side; graph-backed relationships | UI-side filtering | No existence disclosure; relationship-aware |
| W6 | Observability | Compose OpenTelemetry + proven backend; add ERP/business-SLO layer | Rebuild Datadog/Grafana | Differentiate on ERP context, not reinvent TSDBs |
| W7 | Console arch | Modular shell + BFF + CQRS read models + typed command pipeline | Monolithic admin app querying services directly | Team scale, fleet performance, one audited action path |
| W8 | Every action | Typed, audited, gated, replayable command | Ad-hoc endpoints per screen | Uniform SoD/guardrails/audit; API parity |
| W9 | Remote/god-tier ops | Governed operations only; no raw DB/SSH; consent+recording+SoD | Direct DB/server access | Blast-radius + insider-risk control; residency |
| W10 | DSAR vs append-only | **Crypto-erasure (per-subject key-shredding)**; ledger/audit hashes preserved | Delete rows (breaks chain) or refuse DSAR | Satisfies GDPR erasure *and* immutable ledger/audit |
| W11 | Read-model lag | Command pipeline re-validates target state at execution (optimistic concurrency/etag) + idempotency keys | Trust the projection the operator saw | Console display isn't a correctness gate; the write path is |
| W12 | BFF identity | Delegated operator identity via token exchange (RFC 8693), never a service principal | BFF service credential | ABAC enforced by each service; a BFF bug can't escalate |
| W13 | Break-glass root of trust | Owner has no unilateral god-tier; M-of-N quorum; approver-unavailable escalation; PDP-independent last-resort | Owner self-approves; fail-closed with no recovery path | No single point of compromise; recoverable without silent bypass |
| W14 | Session recordings | In-region, encrypted, tenant-attributed, hash-chained; viewing is ABAC+SoD-gated & audited; recording-failure fails the session closed | Global recording pool; unrecorded elevated access | Recordings are tenant PII → residency + no unrecorded god-mode |

(Additive to and consistent with A/P/G/K/D/T/X ADRs in the prior documents.)

---

## 16. Roadmap (phased, reversible, grounded in current code)

Current base: the Control Center already has operator auth, RBAC-ish roles, deny-by-default flags, Developer Mode, support-session impersonation, audit, and read endpoints — the seeds of this workspace.

1. **Governance & command spine (Critical).** Operator IAM (roles+ABAC+MFA+SoD+break-glass) + the typed command pipeline + audit + **API/CLI parity** from day one. Everything else rides these.
2. **Mission Control + Universal Search + Customer 360.** The daily-driver triad, over Decision Center + graph + read models.
3. **Change/Bulk-Ops Safety Engine + Deployment Center + Remote Ops.** Safe fleet action + release control + governed remote hands.
4. **Compliance, Security, Super Admin, Incident Command, Notification/On-call, Data Governance/DSAR.** The rest of the Critical tier.
5. **High tier:** AI, Integration, Marketplace, Financial, Observability (OTel), Knowledge, Intelligence surface, Guardrails, Audit/Forensics, FinOps, Runbooks, Trust/Status, Operator Productivity.
6. **Medium/Future:** Customer Success, Quotas, Localization, Sandbox/what-if, Mobile companion.

Each Center ships behind flags, dark-launchable, independently deployable; the shell + IAM + command pipeline are the only hard prerequisites.

---

## 17. Adversarial architecture review — challenges & revisions

The draft was stress-tested; the blueprint above reflects the outcome.

- **"A super-account is the whole point."** *Challenged and reversed:* one workspace, many least-privilege identities; the god-login is the platform's biggest risk (W2).
- **"Bulk actions are just admin convenience."** *Challenged:* they are the top outage/compromise vector → the Safety Engine with preview/staging/halt/approval/rollback is now mandatory and the *only* bulk path (W3, §4).
- **"Build our own observability."** *Challenged:* reinventing Datadog wastes years and loses; compose OTel + a proven backend, differentiate on ERP/business-SLO context (W6).
- **"The console does the work."** *Challenged:* the console must be a client of the API or ops can't automate and DR is impossible; API/CLI/IaC parity is now Critical and foundational (W1, M3).
- **"The dashboard shows numbers."** *Challenged:* a numbers wall doesn't answer 'what needs me'; it's an attention queue over the intelligence layer (W4).
- **"Search everything" ignored authorization.** *Fixed:* server-side permission-trimming; existence never disclosed (W5).
- **Missing operational spine.** *Added:* Incident Command, Notification/On-call, Runbooks, Data Governance/DSAR, Policy Guardrails, Audit/Forensics, FinOps, Trust/Status, Quotas, Localization, Operator Productivity, API/CLI/IaC parity — without which "mission control" is a façade (§8).
- **"Remote ops = log into the box."** *Challenged:* only governed operations, no raw DB/SSH, consent + recording + SoD, in-region (W9).
- **Fault isolation of the console itself.** *Added:* a Center backend failing must not take down the shell or other Centers; read models keep the console useful during write-path incidents (§13).
- **Ledger repair via admin tools.** *Reaffirmed:* compensating entries only, DB-enforced append-only — never raw SQL, even for the owner (§7 Super Admin).

**Second independent red-team (post-draft) — additional findings incorporated:**
- **"DSAR erasure contradicts the append-only ledger + hash-chained audit."** *Accepted — the most important gap.* Added **crypto-erasure / key-shredding** so PII is destroyed while ledger/audit integrity is preserved; dedicated Data Governance/DSAR section + ADR W10 (§7a).
- **"A stale read model can make an operator cause harm."** *Accepted.* The **command pipeline re-validates target state at execution (optimistic concurrency) + idempotency keys** — the write path is the correctness gate, not the display (ADR W11, §2).
- **"Session recordings are tenant PII with no storage/residency/access spec."** *Accepted.* Recordings are **in-region, encrypted, hash-chained, SoD-gated to view; recording failure fails the session closed** (ADR W14, §7a).
- **"Who approves the Owner's own break-glass? What if the approver is asleep? Fail-closed PDP = unrecoverable?"** *Accepted.* **M-of-N quorum, no unilateral Owner god-tier, approver-unavailable escalation, and a PDP-independent last-resort path** (ADR W13, §3).
- **"Bulk-ops lacked idempotency, concurrency, cross-cell coordination, dry-run fidelity."** *Accepted.* Added **per-target idempotency keys, conflict detection + leases, a cross-cell saga/coordinator, and ring-entry re-validation** (§4).
- **"The BFF could become an over-privileged single point of bypass."** *Accepted.* BFF uses **delegated operator identity (RFC 8693 token exchange)**, never a service principal (ADR W12, §2).
- **"Mission Control fully coupled to the Decision Center — an outage looks like all-clear."** *Accepted.* Added a **raw critical-signal fallback tier** and an explicit **"intelligence degraded"** state (§5).
- **"Real-time streaming needs per-operator ABAC filtering, not just search."** *Accepted.* **Pre-computed per-operator subscription filters** re-evaluated on grant/revoke with live-socket revocation (§2).
- **"Single-target concurrency/locking and operator presence were missing."** *Accepted.* Added **advisory leases per (tenant, resource) + presence** (§2, §7a Remote Ops).
- **"Remote Ops, Incident Command, Data Governance are genuinely different and can't inherit the template; Super Admin needs per-operation gating."** *Accepted.* Added **full dedicated sections + a per-operation god-tier classification table** (§7a).
- **"'Full IaC parity' overclaims for god-tier; search can leak via counts/timing; least-privilege undermined by pre-emptive over-scoping."** *Accepted.* Scoped the parity claim (W1), trimmed **counts/timing**, and added a **zero-knowledge escalation request** (§6).

---

## 18. Closing

The Platform Owner Workspace is **one workspace, least-privilege authority, governed commands, prioritized attention, and no raw access to anything** — a real mission-control layer over the ERP operating system rather than an admin panel. Every consequential assumption was challenged: the super-account became a governed operator organization, bulk actions became safe orchestrated jobs, the console became a scriptable API client, the dashboard became an intelligence-driven attention queue, and the missing operational spine (incidents, on-call, governance, guardrails, FinOps, DSAR, API parity) was added. It stays inside every platform invariant (two-plane, no-data-descends, residency, deny-by-default, fail-closed, tamper-evident, append-only ledger, SoD) and is structured — modular shell + BFF + CQRS read models + one audited command pipeline — to operate tens of thousands of ERP customers across many countries for the next fifteen years without a fundamental redesign.
