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
    claims = {"sub": user.id, "role": user.role, "realm": realm, "company_id": cid, "exp": exp}
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

def scope_branches(user: models.User, db: Session):
    return P.allowed_branches(user, all_branch_names(db))

def assert_branch(user: models.User, db: Session, branch: str):
    if branch not in scope_branches(user, db):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Branch not permitted: {branch}")

def audit(db: Session, user, action, entity, ref="", detail="", source="WEB", result="ok"):
    db.add(models.AuditLog(source=source, user_id=getattr(user, "id", None),
                           action=action, entity=entity, ref=str(ref), detail=detail, result=result))
    db.commit()
