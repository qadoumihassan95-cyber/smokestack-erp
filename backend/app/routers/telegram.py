"""Telegram account-linking endpoints — consumed by the SmokeStack Telegram
worker (postgres/http adapter). Issue is authenticated (a user links their own
account); verify redeems a one-time code and binds the Telegram id."""
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from ..config import settings
from .. import models, security as S
from ..schemas import LinkVerifyIn

router = APIRouter(prefix="/api/telegram", tags=["telegram"])

@router.post("/link/issue")
def issue(db: Session = Depends(get_db), user: models.User = Depends(S.get_current_user)):
    code = f"{secrets.randbelow(1000000):06d}"
    db.add(models.LinkCode(code=code, user_id=user.id,
                           expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.link_code_ttl_min)))
    db.commit()
    S.audit(db, user, "issue_link_code", "telegram", code)
    return {"code": code, "ttl_minutes": settings.link_code_ttl_min}

@router.post("/link/verify")
def verify(body: LinkVerifyIn, db: Session = Depends(get_db)):
    rec = db.get(models.LinkCode, body.code.strip())
    now = datetime.now(timezone.utc)
    exp = rec.expires_at if rec else None
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if not rec or rec.used or (exp and exp < now):
        raise HTTPException(400, "Invalid or expired code")
    rec.used = True
    db.merge(models.TelegramLink(tg_id=body.tg_id, user_id=rec.user_id, device=body.device,
                                 expires_at=now + timedelta(days=7)))
    db.commit()
    u = db.get(models.User, rec.user_id)
    S.audit(db, u, "link", "telegram", body.tg_id, source="TELEGRAM")
    return {"ok": True, "user": {"id": u.id, "name": u.name, "role": u.role, "branches": u.branch_names or None}}

@router.get("/session/{tg_id}")
def session(tg_id: str, db: Session = Depends(get_db)):
    link = db.get(models.TelegramLink, tg_id)
    if not link:
        return {"linked": False}
    u = db.get(models.User, link.user_id)
    return {"linked": True, "user": {"id": u.id, "name": u.name, "role": u.role, "branches": u.branch_names or None}}
