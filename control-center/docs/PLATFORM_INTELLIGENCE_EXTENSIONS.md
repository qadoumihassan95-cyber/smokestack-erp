# PFS Platform Intelligence Extensions

**Document type:** Architecture extension (Chief Architect). Extends — does not replace — `PLATFORM_OS_ARCHITECTURE.md` and `ARCHITECTURE_THREE_STAGE.md`. Every ADR, invariant, and integration point in those documents remains authoritative.
**Adds four first-class platform services:** Business Knowledge Graph, Knowledge Center, Decision Intelligence Center, Digital Twin.
**Design bias (unchanged):** scalability, reliability, maintainability, security, extensibility, DX over speed or simplicity.

> This document was written, then subjected to an adversarial architecture review, then revised. §12 records the challenges raised and how the design changed. What follows is the post-revision blueprint.

---

## 1. The unifying idea: the Intelligence Stack

These four capabilities are not four dashboards. They compose into one layered intelligence system that sits *across* the existing platform, not beside it:

```
        ┌───────────────────────────────────────────────────────────────┐
        │  DECISION INTELLIGENCE CENTER  (the brain — decides & advises)  │
        │   deterministic policy DECIDES · AI SUMMARIZES/RANKS · explains │
        └───────▲───────────────▲──────────────────▲────────────────▲────┘
                │ evidence       │ facts+citations   │ foresight       │ signals
        ┌───────┴──────┐ ┌───────┴────────┐ ┌────────┴───────┐ ┌───────┴────────┐
        │ BUSINESS      │ │ KNOWLEDGE      │ │ DIGITAL TWIN   │ │ Existing platform│
        │ KNOWLEDGE     │ │ CENTER         │ │ (simulation    │ │ Validation·Health│
        │ GRAPH         │ │ (authoritative │ │  foresight     │ │ Telemetry·Rollout│
        │ (semantic     │ │  cited facts,  │ │  gate)         │ │ Licensing·Packs  │
        │  substrate)   │ │  RAG source)   │ │                │ │ AI Runtime       │
        └───────────────┘ └────────────────┘ └────────────────┘ └──────────────────┘
```

- The **Knowledge Graph** is the semantic *substrate* — the relationships and dependencies across the whole platform.
- The **Knowledge Center** is the authoritative *memory of facts* — cited, versioned, effective-dated legal/accounting/operational knowledge.
- The **Decision Intelligence Center** is the *brain* — it fuses graph + knowledge + live signals + simulation results, and produces explainable decisions where **deterministic policy decides anything that touches correctness**, while AI only summarizes, ranks, and explains.
- The **Digital Twin** is *foresight* — it simulates a change before Production and hands risk/impact evidence to the brain.

Everything the brain produces is **evidence-linked and explainable — no black-box decisions**, which is the single hardest requirement and the organizing constraint of this whole extension.

---

## 2. Assumptions I am challenging (before designing)

You asked me to challenge, reorder, and merge where a stronger design exists. The consequential divergences, each carried into the design below:

1. **"PFS must evolve from relational tables into a business knowledge graph."** *Rejected as stated.* The graph must **not** become the system of record. Ledgers and platform state stay in the relational/append-only stores (ACID, the source of truth). The Knowledge Graph is a **derived, rebuildable read-model (CQRS projection)** fed by the event backbone. Making a graph DB your OLTP for financial data trades away transactional integrity, mature backup/restore, and auditability for query convenience — an unacceptable trade for a ledger of record. The graph is an *index over* truth, not truth.

2. **"Digital Twin behaves like a realistic ERP copy" using masked/representative/real data.** *Conflicts with your own invariant* that real customer data never descends to lower stages (residency + blast radius). Resolved with **two twin modes**: (a) a **platform synthetic twin** near Validation using synthetic/representative data only, and (b) a **tenant shadow twin** that runs *inside the tenant's own production cell and region* against an ephemeral point-in-time snapshot — the simulation goes *to* the data, in-region, and only metrics/risk come back; data never descends or leaves its region. This is the only way to be both "realistic" and residency-safe.

3. **"AI must never hallucinate legal rules; every recommendation references approved documents."** RAG-with-citations is necessary but **not sufficient** — grounding reduces but does not eliminate hallucination. The real guarantee: **legal/accounting rules are executed by deterministic compliance packs**, authored and reviewed *from* approved Knowledge Center documents; AI **cites** the knowledge and **abstains** when no approved, effective-dated source supports a claim (a hard citation gate, not a prompt instruction). AI never *is* the rule; the pack is.

4. **"Only after passing the Digital Twin should a release continue to Validation."** *Reordered.* Correctness Validation (deterministic, cheap) must run **first**; you do not spend expensive simulation on code that isn't even correct. The Twin runs **after core Validation passes and before Release-Candidate sign-off**, producing the risk/impact evidence the Decision Center needs to decide *whether and to whom* to ship. Order: `Dev → Internal Testing → Validation (correctness) → Digital Twin (impact/risk) → RC → Signed → Rollout`.

5. **Two graphs would be a mistake.** The Decision Center's "Evidence Graph" and the Knowledge Center's "citation/lineage graph" are **not separate stores** — they are *views over the single Business Knowledge Graph*. One graph, many queries. Building three graphs would guarantee drift. (Merge justified in §12.)

6. **Build order.** The prompt lists Decision (1), Knowledge (2), Twin (3), Graph (4). *Recommended order: Graph → Knowledge → Decision → Twin*, because trustworthy, explainable decisions require the evidence substrate and authoritative facts to exist first, and the Twin's value is realized only once the Decision Center can consume its output. A **thin Decision spine** is stood up early as the integration seam and grows as the substrate matures (§11).

---

## 3. Capability A — Business Knowledge Graph (semantic substrate)

**Purpose.** A platform-wide semantic model of *relationships* — who owns what, what depends on what, what governs what, what impacts what — enabling dependency/impact/root-cause analysis, similarity, risk/compliance propagation, and AI context. It is the substrate the other three services query.

**Architecture.** A **property graph** maintained as a **derived read-model**: the transactional/append-only stores remain the source of truth; a **Graph Projector** consumes domain events (transactional outbox → broker) and updates the graph asynchronously. It is **bitemporal** (valid-time + transaction-time) so any relationship can be queried "as of" a date — essential for audit and compliance. **Two tiers, to honor the two-plane + residency invariants:**
- **Control-plane platform graph** (metadata only): customers, companies, licenses, entitlements, ERP products, marketplace modules, compliance packs, feature flags, releases, deployments, AI agents, integrations, support sessions, knowledge documents, and their relationships. This is what powers platform-wide impact/risk propagation.
- **Tenant business graph** (**opt-in, off by default**, inside each tenant's cell/region): the tenant's own entities (accounts, transactions, suppliers, products, employees, bank accounts) for deep in-tenant BI. Because this is a *second derived copy of real financial data*, it is treated as `restricted`: it inherits the ledger's backup, residency, encryption, and access controls, is minimized to what BI requires, is opt-in per tenant, and is subject to the same erasure/export rights as the ledger. It never leaves the cell. Cross-tenant rollups (owner analytics) are permitted only through an **aggregation/k-anonymity threshold** (minimum cohort size, suppressed small cells) so no rollup can re-identify a tenant — an explicit disclosure control, not a promise of "anonymized."

**Domain model.** *Node types* (platform tier): Customer, Company, Branch, User, License, Entitlement, ErpProduct, Module, MarketplaceModule, CompliancePack, FeatureFlag, Release, Deployment, AiAgent, Integration, SupportSession, KnowledgeDocument. *Edge types:* `owns, operates, licensed_for, entitled_to, installed_as, depends_on, governed_by, deployed_to, cites, impacts, propagates_risk_to, similar_to, supersedes`. Each node/edge carries `valid_from/valid_to`, `tenant_id` (partition key), and a `classification`.

**Services.** Graph Projector (event→graph, idempotent, replayable); Graph Query Service (traversal API behind a `GraphQuery` port); Impact/Dependency Analyzer; Similarity Engine; Risk/Compliance-Propagation Engine; Graph Admin (rebuild/reconcile/repair).

**Data model & storage (recommended).** Start on **PostgreSQL + Apache AGE** (graph over the database you already run) to avoid premature new infrastructure — consistent with the platform's modular-monolith-then-extract stance. Extract to a dedicated graph engine (Neptune/Neo4j/JanusGraph) **only when traversal scale demands it**; the `GraphQuery` port makes that swap mechanical. **The graph inherits the tenant's tenancy tier:** a silo/schema-isolated ledger tenant gets an isolated graph store (not a shared partition), so the platform's most sensitive tenants never receive *weaker* isolation in the intelligence layer than in their ledger; pool-tier tenants share a partitioned store with RLS-equivalent enforcement. Citation/provenance edges are **hash-chained** to match the audit trail's integrity, even though general business nodes are not.

**Events.** Consumes all domain events. Emits `GraphNodeUpserted`, `RelationshipDiscovered`, `RiskPropagated`, `GraphReconciled`.

**APIs.** `query(pattern)`, `neighbors(node)`, `path(a,b)`, `impact(node, depth)`, `dependencies(node)`, `dependents(node)`, `similar(node)`, `subgraph(tenant|scope, asOf)`, `propagateRisk(seed)`.

**Permissions.** Tenant-scoped by default (traversal cannot cross `tenant_id`). Cross-tenant queries are **owner-only and anonymized/aggregated**. ABAC by node classification; knowledge/document nodes follow Knowledge Center permissions.

**Security.** Hard partition by tenant; no cross-tenant traversal path exists in the query layer; PII/`restricted` nodes gated by classification; tenant graphs stay in-region. Graph queries over sensitive subgraphs are audited.

**Audit.** Projector lineage (which event produced which node version); sensitive-subgraph queries recorded; bitemporal history is itself an audit asset ("what did we believe about this customer on that date").

**Scalability.** Async projection decouples write path; graph sharded per cell; hot subgraphs and precomputed metrics (centrality, risk scores) cached; bounded traversal depth with query budgets.

**Caching.** Materialized impact/dependency/risk views, invalidated by `GraphNodeUpserted`/`RelationshipDiscovered` events; hot-node adjacency cache.

**Freshness semantics (correctness-critical — corrected after review).** A single global watermark is *not* sufficient for a gating decision: per-partition lag means the global watermark can read "fresh" while the one event relevant to *this* decision is unprojected. Therefore: **(1)** correctness gates never trust a global watermark — they either read **authoritative relational state** for the specific facts, or require **causal/read-your-writes consistency** (the projector offset for the relevant aggregate ≥ the offset of the event that triggered the decision), using per-partition high-water marks carried on the existing outbox. **(2)** The graph publishes a **max projection-lag SLO** (target: p99 < 5 s); breaching it raises a platform alert. **(3)** A projector outage must not silently freeze all releases: sustained lag trips an **audited break-glass** that routes gating reads to authoritative relational state (slower, but live), rather than fail-closed-blocking the entire pipeline. Advisory analytics may use the eventually-consistent graph freely; only correctness gates carry the causal requirement.

**Failure modes & recovery.** *Divergence from source:* periodic reconciliation + full **rebuild from the event log/snapshots** — being derived is the key resilience property; the graph is always reconstructable and is never a single point of data loss. *Recovery targets:* graph RPO ≈ 0 (rebuildable from the durable event log), RTO bounded by projector drain rate; a **drain-time budget** is monitored so backlog recovery is predictable. Corrupt-projection detection triggers targeted re-projection of affected partitions rather than a global rebuild.

**Performance.** Precomputed centrality/risk; traversal-depth caps; read-replicas for query load; SLOs on `impact()`/`path()` latency.

**Future evolution.** Graph-neural-network embeddings for similarity, fraud, and customer-cohorting; feeding tenant subgraphs to AI agents as structured context; temporal reasoning ("risk trend"). **Constraint:** ML-derived edges (`similar_to`, embedding-inferred links) and any ML risk score are **advisory only and are never inputs to a fail-closed gate** — deterministic gates traverse only deterministic, event-sourced edges. This keeps statistical inference out of correctness decisions (the §9 boundary).

**Trade-offs.** Eventual consistency and a new query paradigm vs relational; mitigated by keeping truth relational and starting on Postgres+AGE. Ops cost of a graph engine deferred until scale justifies it.

**ADR.** G1: graph is a derived read-model, never the OLTP/source of truth. G2: bitemporal property graph. G3: two-tier (platform-metadata graph + in-cell tenant graph) to preserve two-plane + residency. G4: Postgres+AGE first, extract behind `GraphQuery` port later. G5: single graph substrate reused as the Decision "evidence graph" and Knowledge "citation graph" (no duplicate graphs).

**Integration points.** Feeds Decision Center (evidence + impact + root cause), AI Runtime (context), Platform Intelligence detectors (risk/compliance propagation), Marketplace (module dependency graph), Licensing/Entitlement (relationship checks), Knowledge Center (documents as nodes; citations as edges).

**Roadmap.** (1) Projector + platform-tier core nodes/edges from existing events. (2) Impact/dependency APIs. (3) Risk/compliance propagation. (4) Tenant business graph in a pilot cell. (5) Embeddings/GNN.

---

## 4. Capability B — Knowledge Center (authoritative, cited facts)

**Purpose.** The single authoritative, versioned, effective-dated, cited knowledge layer: IRS/government publications, tax and payroll regulations, accounting standards, country laws, SOPs, ERP/API/developer docs, training, migration guides, architecture docs, release/support knowledge. It is the source that compliance packs are authored from and that AI must cite.

**Architecture.** Immutable **versioned document store** + **metadata catalog** + **hybrid retrieval index** (vector embeddings *and* keyword/BM25) + **citation & lineage** (materialized as `KnowledgeDocument` nodes/`cites` edges in the Knowledge Graph, §5). Ingestion pipeline: `upload → malware/PII scan → parse/OCR → chunk → embed → index → review workflow → approval → publish (effective-dated, signed)`. Retrieval is **as-of + jurisdiction aware**: reasoning about a 2023 Texas transaction retrieves the rule effective in 2023 for Texas.

**Domain model.** `KnowledgeDocument{id, version, title, authority, jurisdiction/country, language, effective_date, expiry_date, approval_status, source_hash, signature, classification}`, `Chunk`, `Embedding{model_version}`, `Citation`, `Collection`, `Category`, `ReviewTask`, `Relationship(supersedes/derived_from/implements)`.

**Services.** Ingestion; Parsing/Chunking; Embedding (versioned); Hybrid Index (vector+keyword); Retrieval/RAG; **Citation Engine** (maps a claim → supporting approved sources or returns *insufficient support*); Review/Approval Workflow; Versioning/Diff; Expiry Sweeper; Lineage.

**The anti-hallucination guarantee (the core requirement).** Two mechanisms, layered:
1. **Rules are code, not prose.** Legal/accounting *rules* are deterministic compliance packs, human-authored and reviewed from approved documents; the pack **records the citation** to the exact document version it implements. Execution is deterministic (§ compliance engine).
2. **AI is grounded, verified, and abstains.** Any AI-generated legal/accounting statement must pass the **Citation Gate**: it must be backed by retrieved, *approved*, *effective-dated*, jurisdiction-matching sources; if none exist, the agent **abstains and escalates** rather than guessing. Answers without citations are blocked at the runtime boundary. **Presence of a citation is necessary but not sufficient** — an agent can cite a valid document that does not actually support the claim ("cited-but-unsupported"). So the gate adds an **entailment check**: an independent verifier step must confirm the source *supports* the specific claim, labeling each statement `supported | insufficient | unverified`. `insufficient`/`unverified` legal-accounting claims are withheld and routed to human review; only `supported` statements are surfaced. The gate stops *ungrounded* and *mis-grounded* output, not just missing-citation output.

**Events.** `DocIngested, DocApproved, DocSuperseded, DocExpired, CitationRecorded, RegulatoryChangeDetected`.

**APIs.** `search(query, filters)`, `retrieve(query, asOf, jurisdiction, lang)`, `cite(claim)→sources|insufficient`, `diff(v1,v2)`, `lineage(doc)`, `submitForReview`, `approve`, `collections`, `expiringSoon`.

**Permissions.** Authoring/approval = owner + compliance-authoring roles (separation of duties: author ≠ approver). Retrieval scoped by collection: platform-global (IRS) vs tenant-private (a company's SOPs, isolated to that tenant). ABAC by classification.

**Security & audit.** Documents **signed and source-verified**; PII scanned on ingest; tenant-private collections isolated and residency-bound; **every citation used in a recommendation or pack is recorded**, so any decision can be traced to the exact approved document version that justified it (tamper-evident, hash-chained with the audit trail).

**Scalability.** Vector index sharded; large PDFs processed async; embeddings **versioned** so a model upgrade triggers controlled re-embedding without corrupting retrieval.

**Caching.** Retrieval cache keyed by `(query, asOf, jurisdiction, embedding_model_version)`; invalidated on `DocApproved/Superseded/Expired`.

**Failure modes & recovery.** *Retrieval miss* → abstain, never fabricate. *Stale embeddings after model change* → versioned embeddings + background re-embed; queries pin a model version. *Index loss* → rebuild from immutable documents (documents are the durable truth; index is derived). Documents backed up + replicated per residency.

**Performance.** Pre-embed on ingest; hybrid-search latency SLO; hot-jurisdiction caches.

**Future evolution.** **Regulatory-change monitoring** (ingest feeds detect new/changed authority documents → `RegulatoryChangeDetected` → Decision Center surfaces "Texas payroll rule changed yesterday" and proposes a pack update — human-reviewed, never auto-applied); AI-assisted *pack drafting* (AI proposes rule changes from a new publication for human review); multilingual authority docs.

**Trade-offs.** Approval workflow deliberately slows ingestion (authority > speed); embedding-model churn managed via versioning; storage/compute for indexes.

**ADR.** K1: knowledge is the cited source of facts; deterministic packs execute, AI cites. K2: **Citation Gate** — no approved source, no AI legal/accounting claim (abstain). K3: bitemporal effective-dating + jurisdiction on all retrieval. K4: hybrid vector+keyword retrieval. K5: versioned embeddings. K6: documents immutable/signed; index derived/rebuildable.

**Integration points.** Compliance packs cite and are authored from it; AI Runtime RAG is grounded here and gated by the Citation Gate; Decision Center consumes `RegulatoryChangeDetected` and citation evidence; Knowledge Graph indexes documents as nodes and citations as edges; Marketplace modules can ship knowledge collections.

**Roadmap.** (1) Ingestion + versioned store + approval workflow + signing. (2) Hybrid retrieval + Citation Gate wired into AI Runtime. (3) Effective-dating/jurisdiction retrieval + pack-citation linkage. (4) Regulatory-change monitoring → Decision Center. (5) AI-assisted pack drafting.

---

## 5. Capability C — Decision Intelligence Center (the brain)

**Purpose.** Turn PFS from a platform that *shows* state into one that *understands* it and *recommends or decides* actions — "block this release," "stop this rollout," "this tenant needs a compliance pack," "this migration is unsafe," "this AI recommendation conflicts with accounting rules" — always with linked evidence and an explanation.

**Architecture — the two-tier decision model (the central design decision).**
- **Deterministic Decision Engine** decides anything touching **correctness, compliance, money, safety, or a pipeline gate**. Its verdicts are authoritative and enforce gates (e.g., "block release: Validation X failed / pack Y not satisfied"). Output is *certain* and carries the cited rule — no confidence score, because it is not a guess.
- **Recommendation Engine (AI)** *summarizes, prioritizes, ranks, and explains* — it surfaces what matters, drafts the narrative, proposes next steps — but **never decides correctness**. Its outputs carry a **confidence score** and are advisory; a human or a deterministic policy approves them.

This split is the whole point: **AI helps the owner see and understand; deterministic policy decides the things that must be right.**

**Typed action space (so "the rule wins automatically" is actually decidable).** For the conflict check to be complete rather than best-effort, every AI recommendation that could touch a governed action is emitted as a **typed, bounded action proposal** (a structured object from a closed schema — e.g., `{action: enable_flag, target, cohort}`), not free-form prose. The deterministic engine can then evaluate *every* proposed action against *all* applicable rules; a conflict ("this proposal violates accounting rule Z / pack Y / entitlement X") is a deterministic finding and the rule wins automatically, with no semantic false-negative gap. Free-form NL is allowed only for *explanation*, never as an actionable instruction. Any proposal that cannot be expressed in the typed schema cannot be auto-actioned and must go to a human.

**Domain model.** `Decision{id, subject_ref, type, mode: deterministic|advisory, verdict, confidence?, risk_score, evidence[], root_cause?, recommended_next_step, impact:{customer, financial, compliance, data_loss, rollback_difficulty}, status, blocked_action?, overrides[]}`, `Recommendation`, `EvidenceItem{source, ref, weight, asOf}`, `Policy`, `RiskScore`, `RootCause`, `ApprovalTask`.

**The Evidence Graph (no black box).** Every decision links to an **Evidence Graph** — a *query result over the single Business Knowledge Graph + Knowledge Center citations + telemetry/health snapshots + Digital-Twin results + audit*. This is not a separate store (§2.5); it is the decision's justification, materialized and immutable. "Explainability" = rendering this evidence + the deterministic rule (or the AI rationale + citations) that produced the verdict. **Scope of the "no black box" claim (corrected after review):** for *deterministic* verdicts, explainability is complete and faithful (the exact cited rule + the evidence it read). For *AI advisory* outputs, explainability means **inputs + citations + confidence + the ranking factors** — it is an honest account of what the recommendation was based on, **not** a mechanistic account of why the model produced it (no such faithful account of an LLM exists). We claim the former fully and the latter honestly; because AI is advisory-only, the weaker AI explainability never gates a correctness decision.

**Cross-tier evidence assembly (residency-safe).** Some decisions need both platform-metadata facts and tenant-graph facts, but the two graph tiers deliberately never cross (§3/G3). Assembly therefore happens in the **tenant's region**: the platform-tier evidence is passed *to* an in-region assembler that joins it with the tenant subgraph and returns only the decision-relevant, classified result — the tenant's graph never leaves its cell. Owner-facing decisions render tenant specifics only to authorized owners under ABAC, aggregated otherwise.

**Data model & storage.** `Decision`, `Recommendation`, `EvidenceItem`, `Policy`, `RiskScore`, `RootCause`, and `ApprovalTask` persist in the control-plane relational store (the source of truth), partitioned per cell and scoped by `tenant_id`; residency-bound tenant specifics stay in-region. **Evidence snapshots are immutable and hash-chained** into the tamper-evident audit at decision time (the snapshot commits to the graph version + evidence digests, so a decision's justification cannot be retroactively altered). Retention: decisions and their evidence are retained per compliance policy (multi-year for regulated decisions). Decisions are **rebuildable** by replaying the decision event log against retained evidence snapshots; the store is a projection with the event log as durable truth.

**Services.** Signal Ingestor (health, telemetry, release metadata, licensing, marketplace deps, twin results, regulatory-change events); Deterministic Policy Engine; Recommendation/Ranking (AI, advisory); Risk Scoring; **Root-Cause Analyzer** (graph traversal to the originating node); **Impact Analyzer** (risk propagation over the graph: customer/financial/compliance impact); Explainability Layer; Approval Workflow; Decision History/Timeline; Blocked-Action Detector.

**Events.** `DecisionRaised, RecommendationIssued, DecisionApproved, DecisionOverridden, ActionBlocked, RollbackRecommended, PackNeeded`.

**APIs.** `decisions(subject|scope)`, `recommend(context)`, `explain(decision)`, `risk(subject)`, `impact(action)`, `rootCause(finding)`, `override(decision, reason)`, `approve(decision)`, `history(subject)`, `timeline(scope)`.

**Permissions & security.** View scoped by ABAC; **override/approve is owner-only with separation of duties for high-impact decisions**; **AI can never self-approve** a decision it recommended. Deterministic verdicts and overrides are hash-chained into the tamper-evident audit.

**Audit.** Complete, immutable decision history: verdict, mode, evidence snapshot, confidence, approver/overrider + reason, resulting action. You can reconstruct *why* any action was taken and *what* justified it, at that point in time (bitemporal).

**Scalability & caching.** Gating decisions (in the pipeline hot path) are computed fast and synchronously; heavy root-cause/impact analyses run async with cached results keyed to graph version and invalidated on relevant graph updates. Risk scores cached and recomputed on new signals.

**Gate-affecting risk is deterministic.** Any risk classification that can *lower* a fail-closed gate — most importantly the Digital Twin "low-risk → skip simulation" fast-path (§6) — is computed **deterministically** from change type, blast radius, and touched-invariants, never from an ML score. ML/statistical risk is advisory-only and can raise attention but never relax a correctness gate. This closes the leak where a learned classifier could decide whether a correctness-relevant gate runs.

**Failure modes & recovery.** *Missing/stale evidence:* for **gating** decisions, **deny-by-default** (an insufficiently-evidenced release does not auto-pass), and gate reads use **causal freshness** — the projector offset for the relevant aggregates must be ≥ the triggering event's offset, or the gate reads authoritative relational state directly (never a possibly-stale global watermark, §3). A sustained projector outage trips the audited break-glass to relational reads rather than freezing the pipeline. *AI/Recommendation engine down:* deterministic decisions and gates keep working; advisory features degrade gracefully (ranking falls back to deterministic priority). *Recovery:* decisions replay from the event log + immutable evidence snapshots; **RPO ≈ 0** (durable event log), **RTO** bounded by replay throughput.

**Performance (quantified targets).** Deterministic gate evaluation **p99 < 500 ms** (synchronous, in the pipeline hot path); `explain()` **p95 < 2 s**; heavy `impact()`/`rootCause()` async with cached results **p95 < 10 s**; advisory recommendations async, best-effort. These are targets to hold as SLOs, not guarantees of a first implementation.

**Future evolution.** Policy-as-code library with versioned, tested policies (policies are artifacts through the same pipeline); learned risk models (advisory only, validated against outcomes); "what-if" decisions driven by the Digital Twin; natural-language decision querying grounded in the Evidence Graph.

**Trade-offs.** Policy-authoring burden and explainability cost are real; accepted because black-box decisions are disqualifying for a financial platform. Deterministic rigidity is offset by the advisory layer for genuine judgment calls (which route to humans, not to AI autonomy).

**ADR.** D1: two-tier decisions — deterministic decides correctness/gates, AI advises. D2: every decision is evidence-linked and explainable (no black box). D3: gating decisions fail closed on insufficient evidence. D4: confidence applies to advice, never to deterministic verdicts. D5: human override with SoD; AI never self-approves. D6: the Evidence Graph is a view over the one Business Knowledge Graph, not a new store.

**Integration points.** The integration hub: consumes Knowledge Graph, Knowledge Center, Digital Twin, Validation/Compliance, Health, Telemetry, Licensing/Entitlement, Marketplace deps, AI Runtime; **produces** verdicts and recommendations to the release pipeline gates, Rollout Manager (stop/rollback), Notification Center, Developer Center, and the existing Platform Intelligence layer (which becomes the *detection* feeding the Decision Center's *decisioning*).

**Roadmap.** (1) Thin decision spine + signal ingest + deterministic gates for release/rollout (integration seam). (2) Evidence Graph + explainability. (3) Risk/impact/root-cause over the graph. (4) Advisory recommendation + confidence + approval workflow. (5) Regulatory-change and pack-needed decisions; what-if via Twin.

---

## 6. Capability D — Digital Twin (foresight & simulation gate)

**Purpose.** Before a release reaches Production, simulate it and estimate risk, performance, migration safety, financial/compliance/customer impact, data-loss risk, rollback difficulty, breaking changes, and dependencies — turning "we think it's safe" into measured evidence for the Decision Center.

**Pipeline placement (reordered, §2.4).** `Development → Internal Testing → Validation (deterministic correctness) → DIGITAL TWIN (impact/risk simulation on the RC candidate) → Release Candidate sign-off → Signed Release → Rollout`. Correctness first (cheap, hard gate); simulation second (expensive, probabilistic, informs *whether/to whom* to ship).

**Two modes (the residency resolution, §2.2).**
- **Platform Synthetic Twin:** an ephemeral, isolated environment near Validation, running the candidate against **synthetic/representative data** (generators calibrated to production *shape and aggregates*, never row values). Used for release-wide simulation: performance, migration mechanics, accounting/inventory/payroll/tax invariant preservation, permission and feature-flag behavior, DR/rollback/scaling.
- **Tenant Shadow Twin:** for tenant-specific previews (this customer's migration/rollback/performance), an ephemeral twin provisioned **inside the tenant's own production cell and region** against a **point-in-time snapshot**; the simulation executes where the data already lives, and **only metrics/risk/findings are returned** — data never descends, never leaves region. Touching tenant data requires the **same consent + audit as a support session**. *This is designed, not asserted (corrected after review):* (1) the snapshot is a **copy-on-write, point-in-time clone in an isolated namespace with no write-back path to the live tenant DB** — release-candidate code runs against a sealed copy and physically cannot mutate production; (2) it runs on **data-plane-owned infrastructure orchestrated (not hosted) by the control plane**, preserving plane separation — the control plane sends the job and receives results, never the data; (3) outputs pass an **output-disclosure control**: only aggregates above a minimum-cohort threshold are returned; a finding may reference *that* an issue exists and its class, never specific account/customer rows; (4) **silo/schema-isolated tenants** get enhanced controls and an explicit opt-out. The letter *and* the intent of "no data descends / residency" hold.

**Domain model.** `Twin{id, mode, source_snapshot_ref, release_ref, scenario_ref, status, cell/region}`, `Scenario`, `SimulationRun{metrics, invariant_results, risk, findings}`, `Prediction{risk, perf, migration_safety, financial_impact, compliance_impact, customer_impact, data_loss_risk, rollback_difficulty, breaking_changes, dependencies}`, `Comparison{before, after, delta}`.

**Services.** Twin Manager (provision/snapshot/reset/destroy ephemeral twins); Simulation Engine (deterministic replay + scenario execution); Scenario Library (reusable, versioned scenarios); Replay Engine (event replay for realistic sequences); Prediction/Risk Model; Comparison Engine (before/after diff); Simulation Reports/Timeline; Twin Health.

**Deterministic vs statistical outputs (corrected after review).** The Twin produces two kinds of finding, and they are not weighed the same. **Correctness/irreversibility findings — data-loss risk, destructive/irreversible migration, breaking schema/API change, double-entry or invariant violation — are DETERMINISTIC and hard-block** (a data-loss finding is not a "risk to score," it fails the gate closed). **Performance, capacity, and scaling estimates are statistical/advisory** and inform cohort/rollout decisions but never silently override a deterministic block. The Twin's output contract separates the two so a probabilistic model can never downgrade a correctness-critical finding.

**Data model & storage.** `Twin`, `SimulationRun`, `Prediction`, and `Comparison` records persist in the control-plane store (scoped per cell/tenant, residency-bound); **shadow-twin runs store only the returned aggregates/findings, never snapshot data**. Simulation results are **immutable and linked as hash-chained evidence** to the Decision Center. Retention per compliance policy; runs are reproducible from `snapshot_ref + release_ref + scenario_ref`. The ephemeral twin compute/storage is destroyed on completion; only the results record persists.

**Events.** `TwinProvisioned, SimulationCompleted, SimulationFailed, RiskEstimated, TwinDestroyed`.

**APIs.** `createTwin(mode, release, snapshot?)`, `runScenario(twin, scenario)`, `report(run)`, `compare(runA, runB)`, `predict(change)`, `resetTwin`, `snapshot`, `destroy`.

**Permissions & security.** Owner/developer-gated. **Tenant Shadow Twin = support-session-grade control**: consent, capability scope, short-lived, session-recorded. Twins are ephemeral and **destroyed after use**; shadow-twin outputs are aggregates/metrics, masked — never raw rows; nothing leaves the cell/region.

**Audit.** Every simulation recorded with inputs (release, scenario, snapshot ref) and outputs (risk/findings); shadow-twin runs audited like impersonation; results are linked as evidence in the Decision Center.

**Scalability & caching.** Twins provisioned on demand per cell and torn down (cost-managed); scenario runs parallelized; baseline/synthetic-twin results cached and reused across releases; the Scenario Library is shared.

**Failure modes & recovery.** *Twin provisioning fails* → the gate **fails closed**: the release cannot skip simulation. *Simulation inconclusive* → risk marked **unknown**, which the Decision Center treats as **high risk** (deny-by-default). *Shadow snapshot unavailable* → fall back to synthetic twin with reduced fidelity, flagged. *Orphaned twins* (teardown failure) → a reaper sweeps and force-destroys ephemeral twins past their TTL, and provisioning is idempotent so a retried twin never leaks a second live copy. Twins are reproducible from `snapshot_ref + release_ref`; **RPO n/a** (twins are ephemeral; results are the durable artifact, backed up with the audit).

**Performance (quantified targets).** Synthetic-twin provisioning budget **< 5 min**; standard simulation suite **< 30 min** (heavy/soak sims async); shadow-twin snapshot clone bounded by copy-on-write (near-instant, no full copy). A **fast-path** may skip expensive sims for changes classified low-risk — but per the Decision Center rule (§5) that classification is **deterministic** (change type/blast radius), and the skip is recorded as evidence, never silent, and never applied to changes touching irreversible/data-loss surfaces.

**Future evolution.** Continuous **production traffic-shadowing** (mirror real traffic to a shadow) for the highest-risk releases; automated **chaos/DR game-days**; ML performance prediction (advisory) validated against actual post-release metrics.

**Trade-offs.** Simulation cost/time and synthetic-data fidelity vs realism; the two-mode design accepts more complexity to satisfy both realism and residency; mitigated by fast-pathing low-risk changes and reusing baselines.

**ADR.** T1: Twin runs *after* deterministic Validation, *before* RC/Production (reordered, justified). T2: two modes — platform synthetic twin + in-region ephemeral tenant shadow twin — to satisfy realism *and* no-data-descends/residency. T3: twins ephemeral, isolated, destroyed. T4: the Twin is a deny-by-default gate (fail closed; inconclusive = high risk). T5: outputs are metrics/risk evidence to the Decision Center, never raw data.

**Integration points.** A release-pipeline gate (Environment Manager provisions twins; Migration Manager supplies migrations; Compliance packs and Feature Flags run inside the sim; Rollout Manager consumes cohort-risk); feeds the **Decision Center** as evidence; uses Health/Telemetry baselines for comparison; Knowledge Graph provides dependency context for breaking-change/impact prediction.

**Roadmap.** (1) Platform synthetic twin + migration/performance simulation as a pipeline gate. (2) Accounting/inventory/payroll/tax invariant simulation. (3) Prediction/risk model feeding Decision Center evidence. (4) Tenant shadow twin (in-region, consented) for tenant-specific previews. (5) Production traffic-shadowing + automated DR game-days.

---

## 7. Cross-capability data & control flow

```
 Events (outbox→broker) ─▶ Knowledge Graph Projector ─▶ [ Business Knowledge Graph ]
 Uploads ─▶ Knowledge Center (ingest→review→approve→index) ─▶ docs as graph nodes + citations
                                                                     │
 Release candidate ─▶ Validation(correctness) ─pass─▶ Digital Twin(risk/impact) ─▶ evidence
                                                                     │                │
 Health/Telemetry/Licensing/Marketplace/Regulatory-change ──────────┼────────────────┤
                                                                     ▼                ▼
                                              [ DECISION INTELLIGENCE CENTER ]
                                    deterministic verdicts (gates) + AI advice (ranked, cited)
                                                                     │
                        ┌────────────────────────────┬──────────────┴───────────────┐
                        ▼                            ▼                                ▼
                 Pipeline gates            Rollout Manager (stop/rollback)     Developer Center /
                 (block/allow release)     + Notification Center               Owner (explain, override w/ SoD)
```

Deterministic policy decides the gates; AI makes the picture legible and ranked; everything is evidence-linked and replayable.

---

## 8. Consolidated integration matrix

| Existing service | Knowledge Graph | Knowledge Center | Decision Center | Digital Twin |
|---|---|---|---|---|
| Three-stage pipeline | dependency context | pack provenance | **gate verdicts** | **new gate after Validation** |
| Validation/Compliance packs | governs edges | packs cite docs | consumes results | runs packs in sim |
| Marketplace | module dep graph | module knowledge | dep-conflict decisions | sim module installs |
| Feature flags | flag→release edges | — | flag-conflict decisions | sim flag states |
| Rollout Manager | cohort graph | — | **stop/rollback recs** | cohort-risk input |
| Licensing/Entitlement | license edges | — | license-inconsistency decisions | — |
| AI Runtime | context substrate | **RAG source + Citation Gate** | AI advice channel | agent-behavior sim |
| Health/Telemetry | risk propagation | — | signal source | baseline compare |
| Audit | query lineage | citation records | **decision history (hash-chained)** | sim run records |
| Support sessions | session edges | tenant SOPs | — | **shadow-twin consent model** |
| Tenant isolation/residency | two-tier graph | tenant-private collections | scoped decisions | **in-region shadow twin** |

---

## 9. Cross-cutting non-functionals (apply to all four)

**Security & tenancy:** everything is deny-by-default, ABAC + classification-aware, tenant-partitioned, residency-bound; cross-tenant intelligence is owner-only and anonymized. **Audit:** all four write to the tamper-evident, hash-chained audit; decisions and citations are provable after the fact. **Scalability:** all four are event-driven read-models/services that scale per cell and are rebuildable from the event log — none is a single point of data loss. **Failure posture:** fail closed on anything gating a release or touching correctness; degrade gracefully on advisory/AI features. **Performance:** hot-path (gates) synchronous and fast; heavy analysis async and cached. **Determinism boundary (the invariant across all four):** AI summarizes, retrieves, ranks, simulates, and explains; **deterministic policy and cited rules decide anything that must be correct.**

---

## 10. Merge / split / promote decisions (as requested)

- **Merge the three "graphs" into one.** The Business Knowledge Graph, the Decision Center's Evidence Graph, and the Knowledge Center's citation/lineage graph are **one substrate with three query surfaces**. Building separate graphs guarantees drift and triples the projection cost. *Justification:* single source of relational truth → single derived graph → many views.
- **Keep Knowledge Center and Knowledge Graph separate but linked.** The Knowledge Center owns *documents, embeddings, retrieval, and approval*; the Graph owns *relationships* (including "doc cites doc," "pack implements doc"). Different concerns, different stores, one link. *Do not* fold document storage into the graph.
- **Digital Twin is a service + pipeline gate, not a fourth stage.** It reuses the Environment Manager and cells; framing it as a new isolated *stage* would duplicate the three-stage model. It is a *facility* attached to the pipeline between Validation and RC.
- **Promote to standalone services early:** the **Decision Center** and **Knowledge Center** are heavy and central enough to be first extraction candidates (like Validation and the AI Runtime); the **graph engine** extracts when traversal scale demands; the **Twin** is inherently a separate provisioning service. All four start as modules behind ports in the modular monolith and extract on the same evidence-driven schedule as the rest of the platform.

---

## 11. Recommended build order (reordered from the prompt, justified) & roadmap

**Order: Graph → Knowledge → Decision → Twin**, with a thin Decision spine stood up early as the integration seam.

- **Phase 0 — Seams.** Thin Decision spine (deterministic release/rollout gates over existing signals) + Graph Projector core (platform-tier nodes from existing events). *Rationale: establish the integration point and the substrate first; both are cheap and unlock everything.*
- **Phase 1 — Knowledge Center.** Ingestion, versioned/signed store, approval workflow, hybrid retrieval, **Citation Gate wired into the AI Runtime**. *Rationale: trustworthy AI and cited compliance require authoritative facts before the brain leans on AI.*
- **Phase 2 — Graph depth.** Impact/dependency/root-cause + risk/compliance propagation; documents/citations as graph nodes/edges. *Rationale: the Decision Center's explainability depends on this.*
- **Phase 3 — Decision Center full.** Evidence Graph, explainability, risk/impact/root-cause decisions, advisory recommendations + confidence + approval workflow, regulatory-change and pack-needed decisions.
- **Phase 4 — Digital Twin.** Platform synthetic twin gate → invariant simulation → prediction feeding Decision evidence → tenant shadow twin (in-region, consented) → traffic-shadowing/game-days.

*Why not the prompt's order (Decision→Knowledge→Twin→Graph):* the Decision Center cannot be trustworthy or explainable without the graph (evidence/impact) and the Knowledge Center (cited facts); and the Twin's output is only useful once the Decision Center exists to consume it. Building the brain before its senses and memory would force a rework — the opposite of "optimize for long-term evolution."

*Accepted risk (called out explicitly):* deferring the **Digital Twin** to Phase 4 means that until then, releases are gated by the existing deterministic Validation/Compliance stage **without** simulation-based foresight. This is acceptable — correctness gating still holds — but it is a real, temporary reduction in pre-production risk visibility, so the Twin's *migration-safety* simulation (the highest-value slice) should be pulled forward into Phase 3 if migration risk is the dominant concern in that window.

---

## 12. Adversarial architecture review — challenges raised and revisions made

The draft was stress-tested against its own strongest objections; the design below reflects the outcome, not the original.

- **"The knowledge graph as system of record will bite you."** *Accepted.* Revised to a derived, rebuildable read-model over relational truth (ADR G1). Prevents loss of transactional/audit integrity for the ledger.
- **"A realistic twin needs real data, which your invariants forbid."** *Accepted.* Introduced the two-mode twin — synthetic platform twin + in-region ephemeral tenant shadow twin — so data never descends or leaves region (ADR T2).
- **"RAG citations don't actually stop hallucination."** *Accepted.* Made deterministic packs the executors and added the hard **Citation Gate** (abstain if unsupported) at the runtime boundary (ADR K1/K2), rather than trusting grounding alone.
- **"Running the Twin before Validation wastes money and gates the wrong thing."** *Accepted.* Reordered: correctness Validation first, Twin second, on the RC (ADR T1).
- **"Three graphs will drift."** *Accepted.* Merged to one substrate with three query surfaces (ADR D6/G5, §10).
- **"A central Decision Center + graph are new SPOFs and hot-path risks."** *Mitigated.* Gating decisions are fail-closed and fast; heavy analysis async; the graph is a cell-partitioned, rebuildable read-model with a freshness watermark; advisory features degrade without blocking correctness. Consistent with the control-plane static-stability rule (P13 in the platform doc).
- **"Deterministic-decides sounds clean but who arbitrates AI-vs-rule conflicts?"** *Resolved.* Such a conflict is itself a deterministic finding; the cited rule wins automatically and the AI recommendation is blocked (§5).
- **"Eventual consistency of the graph could make a decision act on stale relationships."** *Mitigated.* Freshness watermark + staleness-as-risk input; gating decisions fail closed when evidence is stale/missing (ADR D3).
- **"Tenant shadow twins are an exfiltration risk."** *Mitigated.* Support-session-grade consent + audit, in-region only, ephemeral, outputs are masked metrics — never rows (ADR T2/T5).
- **"Embedding-model upgrades silently corrupt retrieval."** *Mitigated.* Versioned embeddings + controlled re-embed; queries pin a model version (ADR K5).

**Second independent red-team (post-draft) — additional findings incorporated:**
- **"A global freshness watermark can't protect a specific gating decision."** *Accepted — the biggest fix.* Correctness gates now use **causal/read-your-writes freshness** (projector offset ≥ triggering-event offset) or read authoritative relational state, plus a max projection-lag SLO and an audited break-glass so a projector outage doesn't freeze the pipeline (§3, §5, ADR G6).
- **"The Citation Gate checks citation presence, not entailment."** *Accepted.* Added an **entailment verifier** (`supported / insufficient / unverified`) so cited-but-unsupported claims are withheld, not just missing-citation ones (§4, ADR K7).
- **"AI can leak into a correctness gate via the fast-path skip and ML risk edges."** *Accepted.* Any risk that can *lower* a fail-closed gate is **deterministic**; ML/similarity edges and scores are advisory-only and never gate (§3, §5, ADR D7).
- **"'The rule wins automatically' is undecidable for free-form AI output."** *Accepted.* AI actionable proposals must use a **typed, bounded action schema** so the deterministic engine evaluates every proposal against all rules — no semantic false-negative (§5, ADR D7).
- **"The shadow twin is asserted-safe, not designed-safe."** *Accepted.* Specified copy-on-write sealed snapshot, no write-back, data-plane-orchestrated-not-hosted, and an output-disclosure threshold; enhanced controls/opt-out for silo tenants (§6, ADR T6).
- **"Data-loss is a correctness property being scored as risk."** *Accepted.* Twin now **deterministically hard-blocks** data-loss/irreversibility/breaking-change; only performance/capacity are statistical/advisory (§6).
- **"'One graph' vs 'two-tier, never crosses' is unreconciled; the tenant graph is a second copy of financial data; rollups can re-identify."** *Accepted.* Added residency-safe cross-tier evidence assembly (in-region), made the tenant business graph opt-in/`restricted` with ledger-grade controls, made the graph inherit the tenant's tenancy tier (silo→siloed graph), and put a k-anonymity threshold on rollups (§3, §5, ADR G6).
- **"'No black box' overclaims for AI outputs; SLOs unquantified; data-model facet thin for Decision/Twin; no RTO/RPO."** *Accepted.* Scoped the explainability claim honestly, quantified gate/twin SLOs, added persistence/retention/rebuild for Decision and Twin, and stated RPO/RTO (§3, §5, §6).

---

## 13. Consolidated ADRs

| # | Decision | Chosen | Rejected | Why |
|---|---|---|---|---|
| G1 | Graph role | Derived, rebuildable read-model | Graph as OLTP/system of record | Preserve ledger ACID/audit; graph is an index over truth |
| G2 | Graph shape | Bitemporal property graph | Non-temporal | "As-of" queries for audit/compliance |
| G3 | Graph isolation | Two-tier: platform-metadata + in-cell tenant graph | Single global graph | Two-plane + residency invariants |
| K1 | Legal/accounting truth | Deterministic packs execute; AI cites | AI infers rules | No black-box legal reasoning |
| K2 | Anti-hallucination | Citation Gate: no approved source → abstain | Trust RAG grounding | Grounding ≠ guarantee |
| K3 | Retrieval | Effective-dated + jurisdiction + hybrid | Latest-only vector search | Correct rule for the date/place |
| D1 | Decision authority | Deterministic decides correctness; AI advises | AI decides | Correctness is non-negotiable |
| D2 | Explainability | Evidence-linked, replayable | Score-only outputs | No black-box decisions |
| D3 | Gating under uncertainty | Fail closed on stale/missing evidence | Fail open | Safety of the release pipeline |
| D6 | One graph | Evidence Graph = view over the graph | Separate evidence store | Prevent drift |
| T1 | Twin placement | After Validation, before RC | Before Validation | Don't simulate incorrect code |
| T2 | Twin data | Synthetic platform twin + in-region shadow twin | Copy real data down | Realism + no-data-descends/residency |
| T4 | Twin gate | Deny-by-default; inconclusive = high risk | Advisory-only twin | Foresight must be a real gate |
| G6 | Graph correctness reads | Causal/read-your-writes freshness + tier-inherited isolation + k-anon rollups | Global watermark; flat partitioning | Per-decision correctness; no weaker isolation than the ledger; no re-identification |
| K7 | Citation Gate | Presence **and entailment** verification (supported/insufficient/unverified) | Citation presence only | Stops cited-but-unsupported, not just uncited |
| D7 | Correctness boundary | Gate-affecting risk deterministic; AI proposals in a typed action schema | ML risk / free-form proposals gating | No ML in a fail-closed gate; conflict detection is complete |
| T6 | Shadow twin safety | Copy-on-write sealed snapshot, no write-back, data-plane-orchestrated, disclosure threshold | Assert "only metrics return" | Designed-safe, not asserted-safe; residency + minimization hold |
| X1 | Build order | Graph → Knowledge → Decision → Twin (+ early Decision spine) | Prompt order | Brain needs senses+memory first |
| X2 | Determinism boundary (all four) | AI summarizes/retrieves/ranks/simulates/explains; policy + cited rules decide | AI autonomy over correctness | The platform-wide invariant |

(Extends A1–A16 and P1–P15 in the prior documents; these are additive and consistent with them.)

---

## 14. Closing

The four capabilities form one **Intelligence Stack** layered onto the approved architecture without altering it: a rebuildable graph substrate, an authoritative cited knowledge memory, a brain that decides deterministically and advises with AI, and a twin that provides foresight as a real pipeline gate. Every consequential assumption was challenged and, where a stronger enterprise pattern existed, changed — graph-as-read-model, two-mode residency-safe twin, citation-gated grounding, reordered simulation, one merged graph, and a build order that puts senses and memory before the brain. The result stays inside every existing invariant (two-plane, no-data-descends, residency, deny-by-default, fail-closed, tamper-evident, deterministic-correctness) and is designed to keep evolving for the next fifteen years without a redesign.
