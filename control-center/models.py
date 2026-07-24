"""PFS Control Center — fleet & platform metadata model (Milestone 1).

Metadata ONLY. This service never stores customer transactional/business data; the
authoritative customer record is owned by the ERP application (ADR-021, ADR-028).
Two-lane lifecycle (ADR-028): platform-owned Master environments (dev/test/prod) publish
immutable Releases that are deployed to customer-facing Customer-Production runtimes.
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
    platform_role = Column(String, default="owner")          # owner | operator
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


class PlatformAuditLog(Base):
    """Immutable Control-Plane audit trail (separate from every ERP's own audit)."""
    __tablename__ = "platform_audit_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_operator_id = Column(String)
    action = Column(String)
    target_type = Column(String)
    target_id = Column(String)
    detail = Column(Text)
    result = Column(String, default="ok")
    at = Column(DateTime(timezone=True), server_default=func.now())
