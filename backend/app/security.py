"""Auth + RBAC: password hashing, JWT, current-user dependency, permission +
branch guards, and an audit helper. Every protected route depends on these."""
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from . import models, permissions as P

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# While an account is still on its temporary password (must_change_password), the
# session may reach ONLY these endpoints — replace the password, read its own
# identity, or sign out. Every other protected route is blocked until the flag is
# cleared. (The temp-password login already proved the current password, so the
# change endpoint is reachable with this restricted session.)
FIRST_LOGIN_ALLOWED = {
    "/api/auth/change-password",
    "/api/auth/me",
    "/api/auth/logout",
}

# bcrypt hashes at most the first 72 BYTES of a password. Rather than let the
# hasher silently truncate (a real security footgun), we validate up front and
# raise a clear error so a caller never believes a >72-byte password was stored
# in full.
BCRYPT_MAX_BYTES = 72

def _check_len(p: str) -> None:
    if len(p.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise ValueError(
            f"Password is too long: bcrypt supports at most {BCRYPT_MAX_BYTES} bytes."
        )

def hash_pw(p: str) -> str:
    _check_len(p)
    return pwd.hash(p)

def verify_pw(p: str, h: str) -> bool:
    # An over-length password can never have been hashed, so it can't match.
    if len(p.encode("utf-8")) > BCRYPT_MAX_BYTES:
        return False
    try:
        return pwd.verify(p, h)
    except Exception:
        return False

# ERP is its own authentication realm (distinct from the PFS Control Center realm
# "pfs"). Every ERP token carries the company it acts for; legacy tokens minted
# before tenancy carry none and resolve to Company #1 during the migration.
REALM = "erp"


def make_token(user: models.User, company_id=None, realm: str = REALM, extra: dict = None) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    cid = company_id if company_id is not None else (getattr(user, "company_id", None) or 1)
    claims = {"sub": user.id, "role": user.role, "realm": realm, "company_id": cid, "exp": exp,
              "tv": int(getattr(user, "token_version", 0) or 0)}
    if extra:
        claims.update(extra)
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_alg)

def get_current_user(request: Request = None, token: str = Depends(oauth2),
                     db: Session = Depends(get_db)) -> models.User:
    cred = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token", {"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
        uid = payload.get("sub")
    except JWTError:
        raise cred
    # A Control Center (PFS) token must never authenticate against the ERP.
    if payload.get("realm") == "pfs":
        raise cred
    user = db.get(models.User, uid)
    if not user or user.status != "active":
        raise cred
    # SESSION REVOCATION (SS-H-011): a token minted before the account's current
    # token_version (password change/reset, deactivation, role/branch change,
    # logout) is refused, so a stolen old JWT cannot outlive its revocation.
    if int(payload.get("tv", 0) or 0) != int(getattr(user, "token_version", 0) or 0):
        raise cred
    # Resolve the tenant: token claim wins (this is also how impersonation targets
    # a company), else the user's own company, else Company #1 (legacy token).
    cid = payload.get("company_id") or getattr(user, "company_id", None) or 1
    user._company_id = cid
    user._impersonation = ({"active": True, "by": payload.get("sa")}
                           if payload.get("imp") else None)
    # Tag the request-scoped session so tenant scoping applies to every ORM query
    # in this request (FastAPI caches Depends(get_db), so this is the same session
    # the endpoint uses).
    try:
        db.info["company_id"] = cid
    except Exception:
        pass
    # expose resolved identity to the observability middleware (same request obj)
    if request is not None:
        try:
            request.state.company_id = cid
            request.state.user_id = user.id
            request.state.impersonation = bool(user._impersonation)
        except Exception:
            pass
    # FIRST-LOGIN LOCKOUT: an account still on its temporary password can reach only
    # the password-change / identity / logout endpoints until it sets a new password.
    if getattr(user, "must_change_password", False):
        path = request.url.path if request is not None else ""
        if path not in FIRST_LOGIN_ALLOWED:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Set a new password before continuing.")
    # POLICY PIPELINE (layers 2-3 + read-only): evaluated ONCE here, the single ERP
    # auth chokepoint. Blocks suspended/archived/provisioning/maintenance, enforces
    # read-only + expired-subscription write-blocking. No-op for active companies.
    from . import policy
    policy.enforce_request(request, user, db)
    return user

def require(*perms):
    """Dependency factory: 403 unless the user has ALL listed permissions."""
    def _dep(user: models.User = Depends(get_current_user)):
        for p in perms:
            if not P.can(user.role, p):
                raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing permission: {p}")
        return user
    return _dep

def all_branch_names(db: Session):
    return [b.name for b in db.query(models.Branch).order_by(models.Branch.name).all()]

def branch_labels(db: Session):
    """Map of internal branch key -> human label (display_name, falling back to the key).
    Display-only: the key is what every relation/permission/history references."""
    out = {}
    for b in db.query(models.Branch).all():
        out[b.name] = (getattr(b, "display_name", None) or b.name)
    return out

def branch_label(db: Session, name):
    """Human label for one branch key, safely falling back to the key itself."""
    if name is None:
        return name
    b = db.get(models.Branch, name)
    return (getattr(b, "display_name", None) or name) if b else name

def scope_branches(user: models.User, db: Session):
    return P.allowed_branches(user, all_branch_names(db))

def assert_branch(user: models.User, db: Session, branch: str):
    if branch not in scope_branches(user, db):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Branch not permitted: {branch}")

def audit(db: Session, user, action, entity, ref="", detail="", source="WEB", result="ok",
          commit: bool = True):
    """Record an audit entry.

    AA-06: pass ``commit=False`` from a financial mutation and commit ONCE at the
    end of the handler, so the source effect and its evidence share a single
    transaction. The default stays ``True`` for the many non-financial callers
    that legitimately audit a read or a already-committed side effect — changing
    that default silently would leave those entries uncommitted.
    """
    db.add(models.AuditLog(source=source, user_id=getattr(user, "id", None),
                           action=action, entity=entity, ref=str(ref), detail=detail, result=result))
    if commit:
        db.commit()


# =============================================================================
# Phase-1 security remediation — centralized, reusable enforcement helpers.
# =============================================================================

# --- A. Central branch-scope resolution (SS-H-001) ---------------------------
def resolve_branches(user: "models.User", db: Session, requested):
    """The single authority that turns a caller-supplied ``branch`` value into the
    set of branch keys a request may touch.

    * global roles (owner/admin/accountant or view_all_branches) → every branch;
      branch-scoped roles → only their assigned keys — both come from
      ``scope_branches`` which is already capped to the caller's allowance.
    * an EXPLICIT branch the caller is not allowed is refused with 403 — never
      silently swapped for another branch (the SS-H-001 defect).
    * ``all``/empty/None resolve to the caller's full allowance, so ``branch=all``
      can never widen a scoped role beyond its assignments.
    * unknown / unassigned / malformed keys all fail closed identically, so the
      response never reveals which branches exist.
    """
    allowed = scope_branches(user, db)
    if requested is None:
        return list(allowed)
    req = str(requested).strip()
    if req == "" or req.lower() == "all":
        return list(allowed)
    if req not in set(allowed):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Branch not permitted")
    return [req]


# --- B. Object-level branch/tenant authorization (SS-H-006) ------------------
def assert_same_company(user: "models.User", obj) -> None:
    """Tenant guard: never read or mutate an object owned by another company."""
    uc = getattr(user, "_company_id", None) or getattr(user, "company_id", None)
    oc = getattr(obj, "company_id", None)
    if uc is not None and oc is not None and uc != oc:
        # 404, not 403 — do not confirm the object exists in another tenant.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


def assert_object_branch(user: "models.User", db: Session, current_branch,
                         dest_branch=None) -> None:
    """Object-level guard (SS-H-006/BOLA): the caller must be able to reach the
    object's CURRENT branch; if the operation moves it, the DESTINATION must be
    reachable too. Authorization is never derived solely from a branch supplied
    by the requester."""
    allowed = set(scope_branches(user, db))
    if current_branch is not None and current_branch not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Not permitted for this record")
    if dest_branch is not None and dest_branch not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Branch not permitted")


# --- C. Field-level financial protection (SS-H-002) --------------------------
# Sensitive numeric fields -> the permission that unlocks them. A user must never
# receive one of these merely because the UI hides it. When the permission is
# missing the key is OMITTED (never returned as a misleading zero).
FINANCIAL_FIELD_PERMS = {
    # cost / COGS / valuation
    "cost": "view_cost", "unit_cost": "view_cost", "avg_cost": "view_cost",
    "cogs": "view_cost", "cogs_today": "view_cost", "costs": "view_cost",
    "costs_today": "view_cost", "inventory_cost": "view_cost",
    "inventory_valuation": "view_cost", "inventory_retail": "view_cost",
    "fifo_cost": "view_cost",
    "cost_breakdown": "view_cost", "cost_series": "view_cost",
    # profit / margin
    "profit": "view_profit", "profit_today": "view_profit", "margin": "view_profit",
    "gross_profit": "view_profit", "potential_profit": "view_profit",
    "profit_series": "view_profit", "profit_trend": "view_profit",
    "costs_trend": "view_cost",
    # payroll
    "payroll": "view_payroll", "payroll_today": "view_payroll",
    "payroll_total": "view_payroll", "gross_pay": "view_payroll",
    "net_pay": "view_payroll", "salary": "view_payroll", "hourly_rate": "view_payroll",
}


def redact_financials(user: "models.User", data):
    """Recursively omit any sensitive financial key the caller's role may not see.
    Returns the same structure with disallowed keys removed (not zeroed)."""
    role = getattr(user, "role", None)
    def walk(o):
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()
                    if not (k in FINANCIAL_FIELD_PERMS
                            and not P.can(role, FINANCIAL_FIELD_PERMS[k]))}
        if isinstance(o, list):
            return [walk(x) for x in o]
        return o
    return walk(data)


# --- D. Central password policy (SS-H-011) -----------------------------------
MIN_PASSWORD_LEN = 10

# Small deny-list of obviously weak / common passwords, compared case-insensitively
# against the stripped value. The length + variety checks catch most others.
WEAK_PASSWORDS = frozenset({
    "password", "password1", "password12", "password123", "passw0rd", "passw0rd1",
    "1234567890", "12345678", "123456789", "0123456789", "qwertyuiop", "qwerty123",
    "letmein123", "welcome123", "iloveyou12", "changeme12", "administrator",
    "abcdefghij", "aaaaaaaaaa", "companyname", "brandname12",
    "1qaz2wsx3e", "test123456", "demo123456", "adminadmin", "demo1234",
})


def validate_password(new, confirm=None, current_hash=None) -> None:
    """The single password policy for EVERY password-setting path (owner-created,
    reset, forced first-login change, self-service change). Raises
    HTTPException(422) with a generic, secret-free message; never a 500 on
    oversized / unicode input. Never logs or echoes the password."""
    if not isinstance(new, str) or not new:
        raise HTTPException(422, "A password is required.")
    if confirm is not None and new != confirm:
        raise HTTPException(422, "Passwords do not match.")
    if len(new) < MIN_PASSWORD_LEN:
        raise HTTPException(422, f"Choose a password of at least {MIN_PASSWORD_LEN} characters.")
    if len(new.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise HTTPException(422, f"Password is too long: use at most {BCRYPT_MAX_BYTES} bytes.")
    if new.strip().lower() in WEAK_PASSWORDS or len(set(new)) < 4:
        raise HTTPException(422, "Choose a stronger, less predictable password.")
    if current_hash is not None and verify_pw(new, current_hash):
        raise HTTPException(422, "The new password must be different from the current one.")


# --- E. Session / JWT revocation (SS-H-011) ----------------------------------
def user_token_version(user) -> int:
    return int(getattr(user, "token_version", 0) or 0)


def bump_token_version(db: Session, user) -> None:
    """Invalidate every JWT previously issued to this account by advancing its
    token version. Called on password change/reset, deactivation, role/branch
    change and logout. A stolen old token no longer validates."""
    try:
        user.token_version = user_token_version(user) + 1
    except Exception:
        return
    db.commit()
