"""Centralized POLICY ENGINE — the single evaluation pipeline for every protected
request:

    Platform → Company status → Subscription status → Application → Module →
    Feature flag → Permission → Branch permission

This module owns the first five layers (generic, business-agnostic). Permission +
branch remain in security.py. Evaluation happens exactly once per request:
company/subscription/read-only is enforced inside security.get_current_user (the
single ERP auth chokepoint); module + feature are enforced by router-level
dependencies. Decisions are cached per company and invalidated immediately on any
state change; every state change writes an immutable platform_audit event.

ADDITIVE + backward compatible: Company #1 (status=active, lifetime subscription,
all modules enabled) evaluates to ALLOW for every action, so nothing changes for
the live business. Only non-active companies / disabled modules / disabled
features / expired subscriptions are ever blocked.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from . import models, security
from .database import get_db
from .observability import request_id_var

log = logging.getLogger("pfs.policy")

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# ---- layer 2: COMPANY status matrix ----------------------------------------
_ALL = dict(login=1, read=1, write=1, export=1, jobs=1, telegram=1, api=1, impersonation=1)
COMPANY_STATUS = {
    "active":       _ALL,
    "trial":        _ALL,
    "read_only":    dict(login=1, read=1, write=0, export=1, jobs=0, telegram=1, api=1, impersonation=1),
    "suspended":    dict(login=0, read=0, write=0, export=0, jobs=0, telegram=0, api=0, impersonation=1),
    "archived":     dict(login=1, read=1, write=0, export=1, jobs=0, telegram=0, api=1, impersonation=1),
    "provisioning": dict(login=0, read=0, write=0, export=0, jobs=0, telegram=0, api=0, impersonation=1),
    "maintenance":  dict(login=0, read=0, write=0, export=0, jobs=0, telegram=0, api=0, impersonation=1),
    # legacy synonym used by early seeds
    "deleted":      dict(login=0, read=0, write=0, export=0, jobs=0, telegram=0, api=0, impersonation=1),
}
COMPANY_DEFAULT = "active"   # unknown/missing company → allow (protects legacy Company #1)

# ---- layer 3: SUBSCRIPTION status matrix (independent of company state) -----
SUBSCRIPTION_STATUS = {
    "trial":     dict(read=1, write=1, export=1, jobs=1),
    "active":    dict(read=1, write=1, export=1, jobs=1),
    "lifetime":  dict(read=1, write=1, export=1, jobs=1),
    "grace":     dict(read=1, write=1, export=1, jobs=1),
    "expired":   dict(read=1, write=0, export=1, jobs=0),   # read-only + export (confirmed policy)
    "cancelled": dict(read=1, write=0, export=1, jobs=0),
    "suspended": dict(read=0, write=0, export=0, jobs=0),
}
SUBSCRIPTION_DEFAULT = dict(read=1, write=1, export=1, jobs=1)

# ---- layer 5: MODULE state --------------------------------------------------
BLOCKED_MODULE_STATES = {"disabled", "hidden", "maintenance"}


class Decision:
    def __init__(self, allowed, http_status=403, reason="", layer=""):
        self.allowed = allowed
        self.http_status = http_status
        self.reason = reason
        self.layer = layer


# ---------------------------------------------------------------- cache
_CACHE = {}          # company_id -> (expiry_ts, company_status, subscription_status)
_TTL_SECONDS = 30


def invalidate(company_id):
    """Drop the cached policy state for a company (called immediately after any
    state change so the next request re-evaluates)."""
    _CACHE.pop(company_id, None)


def _states(db: Session, cid: int):
    now = time.time()
    hit = _CACHE.get(cid)
    if hit and hit[0] > now:
        return hit[1], hit[2]
    c = db.get(models.Company, cid)
    company_status = (c.status if c and c.status else COMPANY_DEFAULT)
    sub = (db.query(models.Subscription)
           .filter(models.Subscription.company_id == cid)
           .order_by(models.Subscription.id.desc()).first())
    sub_status = (sub.status if sub and sub.status else "active")
    _CACHE[cid] = (now + _TTL_SECONDS, company_status, sub_status)
    return company_status, sub_status


def _active_overrides(db: Session, cid: int):
    now = datetime.now(timezone.utc)
    rows = (db.query(models.PolicyOverride)
            .filter(models.PolicyOverride.company_id == cid,
                    models.PolicyOverride.active.is_(True)).all())
    out = []
    for o in rows:
        exp = o.expires_at
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp is None or exp > now:
            out.append(o)
    return out


# ---------------------------------------------------------------- evaluation
def evaluate(company_status, sub_status, action, is_impersonation=False, overrides=None):
    # Emergency override (audited, auto-expiring) can only ALLOW, never expand
    # beyond the requested action; financial integrity is enforced separately.
    for o in (overrides or []):
        if o.allow and o.action in (action, "all"):
            return Decision(True, reason=f"override:{o.reason or ''}", layer="override")

    cs = COMPANY_STATUS.get(company_status, COMPANY_STATUS[COMPANY_DEFAULT])
    # Impersonation may always READ (so a Super Admin can inspect a blocked
    # company); writes still follow the matrix + override.
    company_ok = True if (is_impersonation and action == "read") else bool(cs.get(action, 0))
    if not company_ok:
        return Decision(False, 403, f"company status '{company_status}' forbids {action}", "company")

    if action in ("read", "write", "export", "jobs") and not is_impersonation:
        ss = SUBSCRIPTION_STATUS.get(sub_status, SUBSCRIPTION_DEFAULT)
        if not ss.get(action, 1):
            return Decision(False, 403, f"subscription '{sub_status}' forbids {action}", "subscription")
    return Decision(True)


# path-prefix → module / feature. Enforced only for AUTHENTICATED requests (this
# runs inside get_current_user), so bot-token endpoints are unaffected.
PATH_MODULE = {
    "/api/inventory": "inventory",
    "/api/attendance": "attendance",
    "/api/licenses": "licenses",
    "/api/chat": "team_chat",
    "/api/schedule": "work_hours",
}
PATH_FEATURE = {
    "/api/assistant": "assistant",
}


def _match_prefix(path, mapping):
    for prefix, val in mapping.items():
        if path == prefix or path.startswith(prefix + "/"):
            return val
    return None


def enforce_request(request, user, db: Session):
    """The single evaluation pipeline for every AUTHENTICATED ERP request — run
    once here inside get_current_user. Layers 2–3 (company + subscription +
    read-only), then layer 5 (module) and layer 6 (feature) by path. Permission +
    branch remain in the route. Raises HTTPException when denied."""
    cid = getattr(user, "_company_id", None)
    if not cid or request is None:
        return
    action = "write" if request.method in WRITE_METHODS else "read"
    company_status, sub_status = _states(db, cid)
    is_imp = bool(getattr(user, "_impersonation", None))

    # layers 2-3: company status + subscription + read-only
    d = evaluate(company_status, sub_status, action, is_imp, _active_overrides(db, cid))
    if not d.allowed:
        log.info("policy_block company=%s action=%s layer=%s rid=%s",
                 cid, action, d.layer, request_id_var.get())
        raise HTTPException(d.http_status, d.reason)

    # layer 5: module state (server-side, even if the frontend calls directly)
    mod = _match_prefix(request.url.path, PATH_MODULE)
    if mod:
        st = module_state(db, cid, mod)
        if st in BLOCKED_MODULE_STATES:
            log.info("policy_block company=%s module=%s state=%s rid=%s",
                     cid, mod, st, request_id_var.get())
            raise HTTPException(403, f"module '{mod}' is {st}")

    # layer 6: feature flag
    feat = _match_prefix(request.url.path, PATH_FEATURE)
    if feat:
        c = db.get(models.Company, cid)
        if not feature_enabled(db, feat, cid, getattr(c, "application_key", None),
                               getattr(c, "industry", None), sub_status, user.id):
            log.info("policy_block company=%s feature=%s rid=%s",
                     cid, feat, request_id_var.get())
            raise HTTPException(403, f"feature '{feat}' is disabled")


def can_login(db: Session, cid: int) -> bool:
    """Layer 2 at login time (get_current_user has not run yet)."""
    if not cid:
        return True
    company_status, _ = _states(db, cid)
    return bool(COMPANY_STATUS.get(company_status, COMPANY_STATUS[COMPANY_DEFAULT]).get("login", 1))


# ---------------------------------------------------------------- module (layer 5)
def module_state(db: Session, cid: int, module_key: str) -> str:
    cm = (db.query(models.CompanyModule)
          .filter(models.CompanyModule.company_id == cid,
                  models.CompanyModule.module_key == module_key).first())
    if cm is None:
        return "enabled"                 # not configured → enabled by default
    if cm.enabled is False:
        return "disabled"
    return getattr(cm, "state", None) or "enabled"


def require_module(module_key: str):
    """Router/route dependency: block if the module is disabled/hidden/maintenance
    server-side — even if the frontend calls the endpoint directly."""
    def _dep(user: models.User = Depends(security.get_current_user), db: Session = Depends(get_db)):
        st = module_state(db, getattr(user, "_company_id", 1), module_key)
        if st in BLOCKED_MODULE_STATES:
            raise HTTPException(403, f"module '{module_key}' is {st}")
        return user
    return _dep


# ---------------------------------------------------------------- feature (layer 6)
def feature_enabled(db: Session, key: str, cid=None, application_key=None,
                    industry=None, sub_status=None, user_id=None) -> bool:
    """Most-specific scope wins: user → company → subscription → industry →
    application → platform. Undefined flag → enabled (features are opt-out)."""
    flags = db.query(models.FeatureFlag).filter(models.FeatureFlag.key == key).all()
    by = {}
    for f in flags:
        by[(f.scope, str(f.scope_ref) if f.scope_ref is not None else None)] = f.enabled
    for scope, ref in (("user", user_id), ("company", cid), ("subscription", sub_status),
                       ("industry", industry), ("application", application_key), ("platform", None)):
        k = (scope, str(ref) if ref is not None else None)
        if k in by:
            return bool(by[k])
    return True


def require_feature(key: str):
    def _dep(user: models.User = Depends(security.get_current_user), db: Session = Depends(get_db)):
        cid = getattr(user, "_company_id", 1)
        c = db.get(models.Company, cid)
        if not feature_enabled(db, key, cid,
                               getattr(c, "application_key", None),
                               getattr(c, "industry", None), None, user.id):
            raise HTTPException(403, f"feature '{key}' is disabled")
        return user
    return _dep


# ---------------------------------------------------------------- audited state changes
def _audit(db, actor, action, company, prev, new, reason=""):
    db.add(models.PlatformAudit(
        super_admin_id=actor, action=action, entity="company", ref=str(company.id),
        company_id=company.id, detail=reason, prev_value=str(prev), new_value=str(new),
        ip=request_id_var.get()))


def change_company_status(db, company, new_status, actor, reason=""):
    prev = company.status
    company.status = new_status
    _audit(db, actor, "company_status_change", company, prev, new_status, reason)
    db.commit()
    invalidate(company.id)
    return company


def change_subscription_status(db, company_id, new_status, actor, reason=""):
    sub = (db.query(models.Subscription).filter(models.Subscription.company_id == company_id)
           .order_by(models.Subscription.id.desc()).first())
    prev = sub.status if sub else None
    if sub:
        sub.status = new_status
    c = db.get(models.Company, company_id)
    if c:
        _audit(db, actor, "subscription_status_change", c, prev, new_status, reason)
    db.commit()
    invalidate(company_id)
    return sub


def set_module_state(db, company_id, module_key, state, actor, reason=""):
    cm = (db.query(models.CompanyModule)
          .filter(models.CompanyModule.company_id == company_id,
                  models.CompanyModule.module_key == module_key).first())
    if cm is None:
        cm = models.CompanyModule(company_id=company_id, module_key=module_key,
                                  enabled=(state == "enabled"), source="local")
        db.add(cm)
    prev = getattr(cm, "state", None)
    cm.state = state
    cm.enabled = (state == "enabled")
    c = db.get(models.Company, company_id)
    if c:
        _audit(db, actor, "module_state_change", c, prev, state, f"{module_key}:{reason}")
    db.commit()
    invalidate(company_id)
    return cm


def set_override(db, company_id, action, allow, reason, super_admin_id, ttl_minutes=60):
    """Emergency PFS override: auto-expiring, audited, visible. Never bypasses
    financial-integrity protections (those live in the transaction layer)."""
    o = models.PolicyOverride(
        company_id=company_id, action=action, allow=allow, reason=reason,
        created_by=super_admin_id, active=True,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes))
    db.add(o)
    c = db.get(models.Company, company_id)
    if c:
        _audit(db, super_admin_id, "policy_override", c, None,
               f"{action}={allow} ttl={ttl_minutes}m", reason)
    db.commit()
    invalidate(company_id)
    return o
