"""Shared-runtime TENANCY — generic company isolation for every ERP application.

This is a core capability, not a business one: it knows only about companies and
the set of tenant-owned tables, never about any specific ERP. It provides:

  * a central tenant-resolution dependency (`current_company_id`) that reads the
    company only from the authenticated session/token — never from the frontend;
  * automatic query scoping: once a session is tagged with a company_id (done in
    security.get_current_user for every authenticated request), every ORM SELECT
    against a tenant-owned table is filtered to that company, and every new
    tenant row is stamped with it. Cross-company reads therefore return nothing
    (the endpoint then answers 404), and cross-company writes are impossible;
  * the secure foundation for "Login As Company" impersonation: a short-lived ERP
    token minted only by an authorized Super Admin, carrying the target company
    and impersonation metadata (never the owner's password).

Untagged sessions (background bootstraps, the platform seed, tests doing direct
setup) are intentionally NOT filtered, so this is purely additive and backward
compatible: only authenticated ERP requests are scoped.
"""
import contextlib
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException
from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from . import models, security
from .config import settings
from .database import SessionLocal


class TenantContextError(RuntimeError):
    """Raised when tenant-owned data is accessed on a STRICT (fail-closed) session
    that has no resolved company context and is not an explicit system context."""

# Tenant-owned tables (must match the q6f7g8h9i0j1 migration). Platform tables
# (companies, applications, modules, company_modules, subscriptions,
# platform_users, platform_audit) are deliberately excluded.
TENANT_TABLES = {
    "branches", "attendance", "users", "user_branches", "products", "stock",
    "movements", "ledger", "employees", "licenses", "purchases", "transfers",
    "customers", "suppliers", "approvals", "clock_events", "audit_log",
    "telegram_links", "link_codes", "validation_runs", "report_recipients",
    "report_deliveries", "company_settings", "chat_rooms", "chat_members",
    "chat_messages", "chat_reactions", "chat_presence", "chat_tasks",
    "chat_announcements", "reminder_settings", "reminder_deliveries",
    "employee_schedules", "schedule_templates", "schedule_exceptions",
    "telegram_delivery_log",
}


def tenant_model_classes():
    """Mapped classes that are tenant-owned (have company_id)."""
    out = []
    for mapper in models.Base.registry.mappers:
        cls = mapper.class_
        if getattr(cls, "__tablename__", None) in TENANT_TABLES:
            out.append(cls)
    return out


# ---- central resolution dependency -----------------------------------------
def current_company_id(user: models.User = Depends(security.get_current_user)) -> int:
    """The ONLY sanctioned source of the caller's company: the authenticated
    session. Never accept a company id supplied by the client. Falls back to
    Company #1 only for authenticated legacy tokens (during migration)."""
    return getattr(user, "_company_id", 1)


def require_company(user: models.User = Depends(security.get_current_user)) -> int:
    """FAIL-CLOSED tenant dependency for new/critical routes: rejects the request
    if no company context could be resolved from the authenticated identity."""
    cid = getattr(user, "_company_id", None)
    if not cid:
        raise HTTPException(400, "No company context on this request")
    return cid


# ---- explicit session context markers --------------------------------------
def set_session_company(db, company_id: int, strict: bool = True):
    """Tag a session so tenant scoping applies to `company_id` (used by workers /
    jobs / reports to act within one company). Clears any system flag."""
    db.info["company_id"] = company_id
    db.info["system"] = False
    db.info["strict"] = strict
    return db


def use_system_context(db):
    """Mark a session as a DELIBERATE, privileged GLOBAL-maintenance context
    (platform seeds, migrations, cross-company admin). Bypasses tenant scoping on
    purpose — the caller is responsible for auditing the operation. This is the
    only sanctioned way to touch all tenants' data."""
    db.info["system"] = True
    db.info.pop("company_id", None)
    db.info["strict"] = False
    return db


def make_strict(db, strict: bool = True):
    """Turn on fail-closed mode for an already-open session."""
    db.info["strict"] = strict
    return db


@contextlib.contextmanager
def system_session():
    """Open a privileged system-context session (auditable global maintenance)."""
    db = SessionLocal()
    use_system_context(db)
    try:
        yield db
    finally:
        db.close()


@contextlib.contextmanager
def tenant_session(company_id: int, strict: bool = True):
    """Open a fail-closed session scoped to one company (for background jobs)."""
    db = SessionLocal()
    set_session_company(db, company_id, strict=strict)
    try:
        yield db
    finally:
        db.close()


def _stmt_touches_tenant(statement, tenant_set) -> bool:
    try:
        for d in statement.column_descriptions:
            if d.get("entity") in tenant_set:
                return True
    except Exception:
        pass
    return False


# ---- global scoping engine --------------------------------------------------
_installed = False


def install_tenant_scoping():
    """Register the session events that enforce isolation. Idempotent."""
    global _installed
    if _installed:
        return
    tenant_classes = tenant_model_classes()
    tenant_set = set(tenant_classes)

    @event.listens_for(Session, "do_orm_execute")
    def _scope_reads(state):
        if not state.is_select:
            return
        info = state.session.info
        cid = info.get("company_id")
        if cid is not None:
            for cls in tenant_classes:
                # `cid` is a closure variable: SQLAlchemy's lambda-criteria system
                # extracts it as a per-execution bind parameter (do NOT bake via a
                # default arg — that caches the first company's value).
                state.statement = state.statement.options(
                    with_loader_criteria(
                        cls, lambda c: c.company_id == cid, include_aliases=True))
            return
        if info.get("system"):
            return  # deliberate privileged global maintenance (audited by caller)
        if info.get("strict") and _stmt_touches_tenant(state.statement, tenant_set):
            raise TenantContextError(
                "tenant read without a company context on a strict session")
        # else: legacy permissive default (untagged worker/seed paths) — to be
        # removed once every path carries an explicit context (Wave C flips this).

    @event.listens_for(Session, "before_flush")
    def _stamp_writes(sess, _ctx, _instances):
        info = sess.info
        cid = info.get("company_id")
        new_tenant = [o for o in sess.new
                      if getattr(o.__class__, "__tablename__", None) in TENANT_TABLES]
        if cid is not None:
            for obj in new_tenant:
                if getattr(obj, "company_id", None) is None:
                    obj.company_id = cid
            return
        if info.get("system"):
            return  # privileged: rows keep their explicit company_id / server default
        if info.get("strict") and new_tenant:
            raise TenantContextError(
                "tenant write without a company context on a strict session")

    _installed = True


# ---- impersonation foundation ("Login As Company") --------------------------
def mint_impersonation_token(target_company_id: int, target_user_id: str,
                             super_admin_id: str, role: str = "owner",
                             ttl_minutes: int = 15) -> str:
    """Mint a SHORT-LIVED ERP token that acts as `target_user_id` inside
    `target_company_id`, stamped with impersonation metadata. Only an authorized
    Super Admin may call this (enforced by the caller). Never uses or reveals the
    company owner's password."""
    from jose import jwt
    exp = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    return jwt.encode(
        {"sub": target_user_id, "role": role, "realm": security.REALM,
         "company_id": target_company_id, "imp": True, "sa": super_admin_id,
         "exp": exp},
        settings.jwt_secret, algorithm=settings.jwt_alg)


# Install on import so scoping is active as soon as the app (or a test) loads it.
install_tenant_scoping()
