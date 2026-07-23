"""Control Center HTTP surface.

Mounted as a self-contained sub-application, so these paths live under the
Control Center's OWN root: /pfs/... when co-hosted, or / when the service is
extracted to its own domain. No path here overlaps the ERP's /api/... surface.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from . import permissions, security, service
from .db import get_pfs_db
from .repository import PlatformRepository

router = APIRouter()


@router.get("/health")
def health():
    """Public liveness probe for the Control Center realm (own health check so an
    extracted service can be monitored independently)."""
    return {"status": "ok", "realm": "pfs", "service": "pfs-control-center"}


@router.post("/auth/login")
def login(form: OAuth2PasswordRequestForm = Depends(),
          db: Session = Depends(get_pfs_db)):
    u = service.authenticate(db, form.username, form.password)
    if not u:
        raise HTTPException(401, "Incorrect Control Center credentials")
    PlatformRepository(db).audit(u.id, "login", "platform_user", u.id)
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
