"""Control Center HTTP surface.

Mounted as a self-contained sub-application, so these paths live under the
Control Center's OWN root: /pfs/... when co-hosted, or / when the service is
extracted to its own domain. No path here overlaps the ERP's /api/... surface.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from . import permissions, ratelimit as RL, security, service
from .db import get_pfs_db
from .repository import PlatformRepository

router = APIRouter()


@router.get("/health")
def health():
    """Public liveness probe for the Control Center realm (own health check so an
    extracted service can be monitored independently)."""
    return {"status": "ok", "realm": "pfs", "service": "pfs-control-center"}


@router.post("/auth/login")
def login(request: Request = None, form: OAuth2PasswordRequestForm = Depends(),
          db: Session = Depends(get_pfs_db)):
    """Control Center sign-in.

    SECURITY (SEC-11). This had neither of the two controls the tenant login has had
    since SS-H-007: no throttle, and an audit row written only on SUCCESS. So the most
    privileged credential in the system — a platform super admin, who reaches every
    tenant — could be attacked without bound and **without leaving any trace**, while
    a cashier's password could not. 30 failed attempts produced 30 × 401 and zero
    `platform_audit` rows.

    Both halves matter and neither substitutes for the other: the throttle bounds the
    attack, and the failure record is what makes an attempt visible to anyone reviewing
    the platform log afterwards. An unrecorded failed login is an attack that leaves
    the system looking untouched.

    The limiter is the Control Center's OWN (`pfs/ratelimit.py`), not the ERP's: this
    sub-app is decoupled so it can be extracted, and importing `app.ratelimit` would
    have broken that. Limits are tighter than the tenant login's — this credential
    reaches every tenant and legitimate sign-ins are rare.
    """
    RL.guard(db, request, form.username)
    u = service.authenticate(db, form.username, form.password)
    if not u:
        RL.note_failure(db, request, form.username)
        # Attributed to the SUPPLIED username, never to a resolved account: recording
        # a failure against a real super-admin id would let the log itself confirm
        # which usernames exist.
        PlatformRepository(db).audit(None, "failed_login", "platform_user",
                                     form.username, detail="invalid credentials",
                                     ip=RL.client_addr(request))
        raise HTTPException(401, "Incorrect Control Center credentials")
    PlatformRepository(db).audit(u.id, "login", "platform_user", u.id,
                                 ip=RL.client_addr(request))
    return {"access_token": security.make_token(u), "token_type": "bearer",
            "user": service.me(u)}


@router.get("/auth/me")
def me(user=Depends(security.require_super_admin)):
    return service.me(user)


@router.get("/overview")
def overview(user=Depends(security.require_super_admin),
             db: Session = Depends(get_pfs_db)):
    role = getattr(user, "role", None) or "super_admin"
    if not permissions.can(role, permissions.CAP_SYSTEM_READ):
        raise HTTPException(403, "Missing capability: system.read")
    return service.overview(db)
