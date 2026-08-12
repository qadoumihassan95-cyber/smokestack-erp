"""PFS Control Center — platform metadata model (Milestone 1.1, accountant model).

Metadata ONLY. This service never stores customer transactional/business data; the
authoritative customer record is owned by the ERP application (ADR-021, ADR-028).

Product mental model (QuickBooks-Accountant + App Store Connect + GitHub Orgs):
    PFS  ->  ERP Product  ->  Customers  ->  Support Session ("Open ERP")
Supporting workspace objects per ERP: Versions (Release), Updates (CustomerDeployment/
Deployment), Licenses, Health, Audit, Settings.

Two-lane lifecycle (ADR-028): platform-owned Master environments (dev/test/prod) publish
immutable Releases ("Versions") that are rolled out ("Updates") to customer runtimes.
`Runtime` remains a *backend* technical entity — it is not a primary navigation destination
in the owner UI; owners think in ERP Products and Customers.

First-class additions in this milestone: `License` and `SupportSession` (both metadata-only).
"""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, func

from database import Base


class Operator(Base):
    """A Control-Plane principal (the Platform Owner; later, staff). Its own realm."""
    __tablename__ = "operators"
    id = Column(String, primary_key=True)                    # e.g. OP-owner
    name = Column(String, nullable=False)
    email = Column(String, unique=True)
    password_hash = Column(String, nullable=False)
    platform_role = Column(String, default="owner")          # legacy coarse role: owner | operator | internal
    # Mission Control operator-org (additive): fine-grained least-privilege roles + ABAC scopes.
    # `roles` = csv of iam roles (platform_owner, operator, support_engineer, release_manager,
    # compliance_officer, billing_admin, security_officer, privacy_officer, incident_commander,
    # read_only_auditor). `scopes` = JSON ABAC scope, e.g. {"erp":["*"],"region":["*"],"env":["*"]}.
    roles = Column(Text, default="")
    scopes = Column(Text, default="")
    mfa_enabled = Column(Boolean, default=False)             # MFA-ready (M2); enforced per policy later
    status = Column(String, default="active")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ErpProduct(Base):
    """A vertical ERP application definition (the versioned/published unit)."""
    __tablename__ = "erp_products"
    id = Column(String, primary_key=True)                    # e.g. "smokestack"
    name = Column(String, nullable=False)
    description = Column(Text)
    status = Column(String, default="active")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class MasterEnvironment(Base):
    """Platform-only lifecycle stage of a product (Master lane). No customer data."""
    __tablename__ = "master_environments"
    id = Column(Integer, primary_key=True, autoincrement=True)
    erp_product_id = Column(String, ForeignKey("erp_products.id"), index=True)
    kind = Column(String, nullable=False)   # master_development | master_testing | master_production
    display_name = Column(String)
    status = Column(String, default="defined")   # M1: defined (no physical runtime yet)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Release(Base):
    """An immutable, versioned artifact. Published only from Master Production (ADR-028).

    Bootstrap exception: the current SmokeStack production build may be registered as an
    Imported Legacy Release (is_legacy_import=True) because it predates the Master lifecycle.
    """
    __tablename__ = "releases"
    id = Column(Integer, primary_key=True, autoincrement=True)
    erp_product_id = Column(String, ForeignKey("erp_products.id"), index=True)
    version = Column(String, nullable=False)
    source_sha = Column(String)
    build_identity = Column(String)
    source_master_runtime = Column(String)   # master-prod runtime it was published from (null for legacy)
    status = Column(String, default="draft")  # draft | published | deprecated | imported_legacy
    is_legacy_import = Column(Boolean, default=False)
    published_at = Column(DateTime(timezone=True))
    published_by = Column(String)             # operator id
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Runtime(Base):
    """A registered running instance. tier=master (Master lane) or tier=customer (Customer Production)."""
    __tablename__ = "runtimes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    erp_product_id = Column(String, ForeignKey("erp_products.id"), index=True)
    tier = Column(String, nullable=False)             # master | customer
    environment_kind = Column(String, nullable=False)  # master_* | customer_production
    name = Column(String, nullable=False)
    url = Column(String)
    health_url = Column(String)
    status = Column(String, default="registered")
    current_release_id = Column(Integer, ForeignKey("releases.id"), nullable=True)
    last_health_state = Column(String, default="unknown")
    last_health_at = Column(DateTime(timezone=True))
    last_health_detail = Column(Text)
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CustomerRef(Base):
    """A *reference* to a customer. The ERP application owns the authoritative record."""
    __tablename__ = "customer_refs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    erp_product_id = Column(String, ForeignKey("erp_products.id"), index=True)
    name = Column(String, nullable=False)
    external_ref = Column(String)    # the tenant/company_id inside the ERP
    status = Column(String, default="active")
    region = Column(String, default="us")            # residency/region (M3 fleet targeting)
    version = Column(Integer, default=1)             # optimistic-concurrency token for bulk ops
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CustomerDeployment(Base):
    """Ties a customer to a published Release running on a Customer-Production runtime."""
    __tablename__ = "customer_deployments"
    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_ref_id = Column(Integer, ForeignKey("customer_refs.id"), index=True)
    release_id = Column(Integer, ForeignKey("releases.id"))
    runtime_id = Column(Integer, ForeignKey("runtimes.id"))
    tenant_ref = Column(String)      # company_id inside the customer DB
    status = Column(String, default="active")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Deployment(Base):
    """An observed record that a Release runs on a Runtime (M1: observe, not orchestrate)."""
    __tablename__ = "deployments"
    id = Column(Integer, primary_key=True, autoincrement=True)
    runtime_id = Column(Integer, ForeignKey("runtimes.id"), index=True)
    release_id = Column(Integer, ForeignKey("releases.id"))
    kind = Column(String, default="customer_deployment")   # master_promotion | customer_deployment
    status = Column(String, default="observed")
    health_at_observe = Column(String)
    observed_at = Column(DateTime(timezone=True), server_default=func.now())


class License(Base):
    """A first-class licence granting a Customer the right to run an ERP Product.

    Metadata only — no billing or payment processing. Statuses: trial | active |
    suspended | expired | cancelled.
    """
    __tablename__ = "licenses"
    id = Column(Integer, primary_key=True, autoincrement=True)
    erp_product_id = Column(String, ForeignKey("erp_products.id"), index=True)
    customer_ref_id = Column(Integer, ForeignKey("customer_refs.id"), index=True)
    plan = Column(String, nullable=False, default="standard")   # e.g. trial | standard | pro | enterprise
    status = Column(String, nullable=False, default="trial")    # trial|active|suspended|expired|cancelled
    start_date = Column(DateTime(timezone=True))
    expiry_date = Column(DateTime(timezone=True))
    seat_limit = Column(Integer)
    branch_limit = Column(Integer)
    notes = Column(Text)
    created_by = Column(String)                                 # operator id
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SupportSession(Base):
    """A first-class, short-lived, capability-scoped, auditable, revocable support grant.

    The "Open ERP" action. It NEVER uses a customer password (ADR-025). Because ERP-side
    session consumption is not yet implemented, a new session is created in the
    `pending_erp_integration` state: the Control Center records the grant, exposes the
    registered customer ERP URL, and audits it — but does not (and must not) authenticate
    into the ERP. Restricted by default: capabilities are minimal unless explicitly widened.
    """
    __tablename__ = "support_sessions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_ref = Column(String, unique=True, index=True)      # opaque, non-authenticating handle
    erp_product_id = Column(String, ForeignKey("erp_products.id"), index=True)
    customer_ref_id = Column(Integer, ForeignKey("customer_refs.id"), index=True)
    operator_id = Column(String)                               # who opened it
    reason = Column(Text)
    capabilities = Column(Text, default="support:read")        # comma-list; restricted by default
    # pending_erp_integration | active | expired | revoked
    status = Column(String, nullable=False, default="pending_erp_integration")
    target_url = Column(String)                                # registered customer ERP URL (metadata)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))
    revoked_by = Column(String)


class FeatureFlag(Base):
    """A first-class feature/visibility flag governing access to an ERP feature or internal tool.

    Deny-by-default: a feature is invisible/inaccessible until its flag explicitly enables it for
    the requesting context. `visibility` sets the audience class; the targeting fields narrow it.
    Metadata only — no secrets are ever stored here. `erp_product_id` NULL means 'all ERP products'.
    """
    __tablename__ = "feature_flags"
    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String, nullable=False, index=True)                 # e.g. "inventory.batch_repair"
    name = Column(String, nullable=False)
    description = Column(Text)
    erp_product_id = Column(String, ForeignKey("erp_products.id"), nullable=True, index=True)
    module = Column(String)                                          # inventory | sales | platform | ...
    # customer | platform_owner_only | internal_team | experimental | disabled
    visibility = Column(String, nullable=False, default="customer")
    default_state = Column(Boolean, default=False)                   # on for everyone (customer vis) if True
    environment_scope = Column(String, default="all")                # all | csv of development,staging,production
    customer_allowlist = Column(Text, default="")                    # csv of customer external_refs
    customer_denylist = Column(Text, default="")
    user_allowlist = Column(Text, default="")                        # csv of user ids
    role_requirements = Column(Text, default="")                     # csv of roles (any-of)
    license_plan_requirements = Column(Text, default="")             # csv of plans (any-of)
    rollout_percentage = Column(Integer, default=0)                  # 0..100 deterministic bucket
    # Release pipeline stage (independent of on/off targeting):
    # development | internal_testing | staging | pilot | production | deprecated | removed
    lifecycle_stage = Column(String, default="development")
    start_date = Column(DateTime(timezone=True))                     # scheduled release (not before)
    expiry_date = Column(DateTime(timezone=True))                    # scheduled expiration
    status = Column(String, default="active")                        # active | archived
    # Optimistic-concurrency token: bumped on every governed mutation. A command carries an
    # `expected_version`; the executor re-validates it against this before applying (ADR W11).
    version = Column(Integer, default=1)
    created_by = Column(String)
    updated_by = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class FeatureFlagAudit(Base):
    """Immutable audit of every feature-flag action, with before/after state and reason."""
    __tablename__ = "feature_flag_audit"
    id = Column(Integer, primary_key=True, autoincrement=True)
    feature_flag_id = Column(Integer, ForeignKey("feature_flags.id"), index=True)
    feature_key = Column(String, index=True)
    erp_product_id = Column(String)
    actor_operator_id = Column(String)
    actor_type = Column(String)                                      # platform_owner | internal | customer | system
    customer_ref = Column(String)
    environment = Column(String)
    action = Column(String)
    before_state = Column(Text)                                      # JSON snapshot
    after_state = Column(Text)                                       # JSON snapshot
    reason = Column(Text)
    at = Column(DateTime(timezone=True), server_default=func.now())


class DevPreviewSession(Base):
    """A Platform-Owner Developer-Mode preview of an ERP for a given customer + environment.

    Backs the 'Developer Mode Enabled' banner and the Preview Started/Ended audit. It is a
    metadata record only — it never authenticates into the ERP and never touches customer data.
    `feature_profile` is the identity the owner previews as (default 'platform_owner').
    """
    __tablename__ = "dev_preview_sessions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    operator_id = Column(String, index=True)
    erp_product_id = Column(String, ForeignKey("erp_products.id"), index=True)
    customer_ref = Column(String)                 # external_ref of the previewed customer (optional)
    customer_name = Column(String)
    environment = Column(String, default="development")   # development | staging | production
    feature_profile = Column(String, default="platform_owner")
    status = Column(String, default="active")     # active | ended
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    ended_at = Column(DateTime(timezone=True))


class PlatformAuditLog(Base):
    """Immutable, tamper-evident Control-Plane audit trail (separate from every ERP's own audit).

    Each row is hash-chained: `entry_hash = sha256(prev_hash + canonical(row))`, so any deletion
    or edit of history is detectable by re-walking the chain (ADR W-audit). Legacy rows written
    before hash-chaining have null chain fields and are treated as the chain genesis.
    """
    __tablename__ = "platform_audit_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_operator_id = Column(String)
    action = Column(String)
    target_type = Column(String)
    target_id = Column(String)
    detail = Column(Text)
    result = Column(String, default="ok")
    # Command-pipeline provenance (additive; unindexed to keep additive migration reversible):
    correlation_id = Column(String)
    command_type = Column(String)
    idempotency_key = Column(String)
    # Tamper-evident hash chain (additive):
    prev_hash = Column(String)
    entry_hash = Column(String)
    at = Column(DateTime(timezone=True), server_default=func.now())


class CommandLog(Base):
    """Durable record of every typed governed command through the Mission Control pipeline.

    The idempotency key is unique: a retried command returns the recorded result instead of
    re-applying (exactly-once). This is the single audited path for every mutation (ADR W8/W11).
    """
    __tablename__ = "command_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    command_type = Column(String, nullable=False, index=True)
    operator_id = Column(String, index=True)
    target = Column(String)
    tenant_context = Column(String)
    environment = Column(String)
    params = Column(Text)                    # JSON
    justification = Column(Text)
    idempotency_key = Column(String, unique=True, index=True)
    expected_version = Column(Integer)
    correlation_id = Column(String, index=True)
    blast_radius = Column(String)            # low | medium | high | irreversible
    approval_policy = Column(String)         # none | single | m_of_n
    approved_by = Column(String)
    status = Column(String, default="requested")   # requested|authorized|executing|completed|failed|rejected|halted
    reason = Column(Text)
    result = Column(Text)                    # JSON
    requested_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True))


# ==========================================================================================
#      MISSION CONTROL M2 — governance depth, event backbone, CQRS, sessions (all additive)
# ==========================================================================================
class ApprovalRequest(Base):
    """A generic approval workflow request (used by break-glass elevation, god-tier commands, …).

    Policies: single (1 approver), m_of_n (quorum distinct approvers), sequential (ordered).
    Separation of duties: the requester can never be a valid approver of their own request.
    """
    __tablename__ = "approval_requests"
    id = Column(Integer, primary_key=True, autoincrement=True)
    subject_type = Column(String, nullable=False)   # elevation | command | god_tier | bulk
    subject_ref = Column(String)
    requested_by = Column(String, index=True)
    policy = Column(String, default="single")       # single | m_of_n | sequential
    quorum_required = Column(Integer, default=1)
    status = Column(String, default="pending")      # pending | approved | rejected | cancelled | expired
    reason = Column(Text)                           # mandatory justification
    correlation_id = Column(String, index=True)
    expires_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    decided_at = Column(DateTime(timezone=True))


class Approval(Base):
    """One approver's decision on an ApprovalRequest (immutable once written)."""
    __tablename__ = "approvals"
    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(Integer, ForeignKey("approval_requests.id"), index=True)
    approver_id = Column(String, index=True)
    decision = Column(String)                       # approve | reject
    reason = Column(Text)
    sequence = Column(Integer, default=0)           # for sequential policy
    at = Column(DateTime(timezone=True), server_default=func.now())


class ElevationGrant(Base):
    """A just-in-time, time-boxed privilege elevation (break-glass). Never a standing privilege.

    Normal path: gated by an M-of-N ApprovalRequest, activated only on quorum. Offline path:
    PDP-independent, unlocked by a separately-held emergency credential, for recovery when the
    live authorization plane is down — maximally recorded and reviewed. Recording failure
    terminates the grant (no unrecorded elevated access).
    """
    __tablename__ = "elevation_grants"
    id = Column(Integer, primary_key=True, autoincrement=True)
    operator_id = Column(String, index=True)
    capability = Column(String)                     # capability/action scope, or "*"
    reason = Column(Text)                           # mandatory
    status = Column(String, default="pending")      # pending | active | expired | revoked | rejected | consumed
    approval_request_id = Column(Integer, ForeignKey("approval_requests.id"), nullable=True)
    offline = Column(Boolean, default=False)
    recording_ref = Column(String)                  # in-region recording handle (metadata)
    correlation_id = Column(String, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    activated_at = Column(DateTime(timezone=True))
    expires_at = Column(DateTime(timezone=True))    # elevated_until (time-box)
    revoked_at = Column(DateTime(timezone=True))
    revoked_reason = Column(String)


class OperatorSession(Base):
    """An operator's authenticated session (opened at login). Revocable and expiring; tracks
    MFA + break-glass state and presence for the workspace."""
    __tablename__ = "operator_sessions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    operator_id = Column(String, index=True)
    jti = Column(String, unique=True, index=True)   # token id this session is bound to
    device = Column(String)
    ip = Column(String)
    mfa_state = Column(String, default="none")      # none | satisfied | required
    break_glass = Column(Boolean, default=False)
    status = Column(String, default="active")       # active | revoked | expired
    recording_ref = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))


class Outbox(Base):
    """Transactional outbox — events written in the SAME transaction as the state change, then
    relayed at-least-once to consumers with retry and a dead-letter terminal state. The durable
    source of truth for the event backbone; a real broker can consume this later without rewrite.
    """
    __tablename__ = "outbox"
    id = Column(Integer, primary_key=True, autoincrement=True)
    aggregate_type = Column(String, index=True)
    aggregate_id = Column(String)
    event_type = Column(String, nullable=False, index=True)
    event_version = Column(Integer, default=1)
    payload = Column(Text)                          # JSON
    correlation_id = Column(String, index=True)
    causation_id = Column(String)
    dedupe_key = Column(String, unique=True)        # idempotent delivery
    status = Column(String, default="pending")      # pending | published | failed | dead
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=5)
    available_at = Column(DateTime(timezone=True), server_default=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    published_at = Column(DateTime(timezone=True))
    last_error = Column(Text)


class ReadModelState(Base):
    """Projection watermark + freshness for a CQRS read model (rebuildable, replayable)."""
    __tablename__ = "read_model_state"
    name = Column(String, primary_key=True)
    last_event_id = Column(Integer, default=0)
    status = Column(String, default="live")         # live | rebuilding | stale
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    rebuilt_at = Column(DateTime(timezone=True))


class RMCommandFeed(Base):
    """First CQRS read model — a denormalized feed of governed command activity, projected
    asynchronously from `command.*` outbox events. Powers the workspace activity/Mission-Control
    Tier-2 raw feed. Fully rebuildable by replaying the outbox."""
    __tablename__ = "rm_command_feed"
    id = Column(Integer, primary_key=True)          # = source outbox event id (idempotent upsert)
    command_type = Column(String, index=True)
    operator_id = Column(String, index=True)
    target = Column(String)
    status = Column(String)
    blast_radius = Column(String)
    correlation_id = Column(String)
    occurred_at = Column(DateTime(timezone=True))


# ==========================================================================================
#           MISSION CONTROL M3 — Bulk-Operations Safety Engine (all additive)
# ==========================================================================================
class Segment(Base):
    """A reusable / dynamic target set. `filters` is a JSON expression resolved live against
    customer references (never 'everything' by accident)."""
    __tablename__ = "segments"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    description = Column(Text)
    filters = Column(Text, default="{}")            # JSON: {erp, region, status, plan, version, ...}
    saved = Column(Boolean, default=True)
    created_by = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ChangeJob(Base):
    """A durable, resumable orchestration of a per-target command across a segment. The ONLY path
    for multi-tenant/fleet actions: segment → preview → blast-radius → approval → canary rings →
    auto-halt → rollback → audit. Never a synchronous bulk button."""
    __tablename__ = "change_jobs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String)
    command_type = Column(String, nullable=False)   # per-target command executed via the pipeline
    params = Column(Text, default="{}")             # JSON
    filters = Column(Text, default="{}")            # resolved segment snapshot (JSON)
    reason = Column(Text)
    blast_radius = Column(String)                   # single|small|large|regional|cross_region|fleet
    data_class = Column(String)                     # metadata|customer_data|financial|security|...
    approval_policy = Column(String, default="none")  # none|single|m_of_n
    approval_request_id = Column(Integer, ForeignKey("approval_requests.id"), nullable=True)
    rollback_required = Column(Boolean, default=False)
    rings = Column(Text, default="[]")              # JSON list of ring target counts
    current_ring = Column(Integer, default=0)
    rate_limit_per_tick = Column(Integer, default=50)
    maintenance_window = Column(Text)               # JSON {start,end} optional
    error_budget = Column(String, default="0.2")    # fraction of a ring that may fail before halt
    # planned|awaiting_approval|approved|running|paused|halted|aborting|aborted|completed|
    # failed|rolling_back|rolled_back
    status = Column(String, default="planned")
    halt_reason = Column(String)
    total_targets = Column(Integer, default=0)
    created_by = Column(String, index=True)
    correlation_id = Column(String, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))


class ChangeTarget(Base):
    """One target within a ChangeJob. Carries the planned before/after state and the
    expected_version for optimistic concurrency; per-target idempotency prevents double-apply."""
    __tablename__ = "change_targets"
    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("change_jobs.id"), index=True)
    target_type = Column(String, default="customer")
    target_ref = Column(String, index=True)         # customer_ref id
    expected_version = Column(Integer)
    planned_state = Column(Text)                    # JSON {before, after}
    ring = Column(Integer, default=0)
    status = Column(String, default="pending")      # pending|running|succeeded|failed|skipped|rolled_back
    attempts = Column(Integer, default=0)
    idempotency_key = Column(String, unique=True)
    result = Column(Text)
    error = Column(String)
    at = Column(DateTime(timezone=True), server_default=func.now())
