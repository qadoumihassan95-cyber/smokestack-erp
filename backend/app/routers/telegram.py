"""Telegram account-linking endpoints.

Web app (authenticated user):
    POST /api/telegram/link-code   -> issue a fresh 6-digit code (5 min, single active)
    GET  /api/telegram/status      -> this user's Telegram link status
    POST /api/telegram/unlink      -> remove this user's Telegram link
    POST /api/telegram/link/issue  -> legacy alias of link-code (kept for compatibility)

Telegram bot (unauthenticated, uses a one-time code):
    POST /api/telegram/link/verify -> redeem a code and bind the Telegram id
    GET  /api/telegram/session/{tg_id} -> resolve a Telegram id to an ERP user (touches activity)
"""
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, security as S
from ..schemas import LinkVerifyIn

router = APIRouter(prefix="/api/telegram", tags=["telegram"])

CODE_TTL_SECONDS = 300  # 5 minutes


def _aware(dt):
    """Treat naive DB datetimes as UTC so comparisons are safe."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt):
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def _issue_code(db: Session, user: models.User):
    """Invalidate any previous unused codes for this user, then mint a new one.
    Guarantees only one active code per user at a time (edge case: multiple browsers)."""
    for old in db.query(models.LinkCode).filter(models.LinkCode.user_id == user.id,
                                                models.LinkCode.used == False).all():  # noqa: E712
        old.used = True
    code = f"{secrets.randbelow(1000000):06d}"
    expires = datetime.now(timezone.utc) + timedelta(seconds=CODE_TTL_SECONDS)
    db.add(models.LinkCode(code=code, user_id=user.id, expires_at=expires, used=False))
    db.commit()
    S.audit(db, user, "issue_link_code", "telegram", code)
    return {"code": code, "expires_at": _iso(expires), "expires_in": CODE_TTL_SECONDS}


@router.post("/link-code")
def link_code(db: Session = Depends(get_db), user: models.User = Depends(S.get_current_user)):
    return _issue_code(db, user)


@router.post("/link/issue")
def issue(db: Session = Depends(get_db), user: models.User = Depends(S.get_current_user)):
    # Legacy shape kept working; also returns ttl_minutes for old callers.
    r = _issue_code(db, user)
    return {**r, "code": r["code"], "ttl_minutes": CODE_TTL_SECONDS // 60}


def _status_for_user(db: Session, user_id: str):
    link = db.query(models.TelegramLink).filter(models.TelegramLink.user_id == user_id).first()
    if not link:
        return {"connected": False}
    return {"connected": True, "tg_id": link.tg_id, "username": link.username,
            "linked_at": _iso(link.linked_at), "last_activity": _iso(link.last_activity),
            "device": link.device, "status": "connected"}


@router.get("/status")
def status(db: Session = Depends(get_db), user: models.User = Depends(S.get_current_user)):
    return _status_for_user(db, user.id)


@router.post("/unlink")
def unlink(db: Session = Depends(get_db), user: models.User = Depends(S.get_current_user)):
    links = db.query(models.TelegramLink).filter(models.TelegramLink.user_id == user.id).all()
    if not links:
        return {"ok": True, "connected": False}
    for link in links:
        db.delete(link)
    # also burn any outstanding codes so nothing can re-link silently
    for c in db.query(models.LinkCode).filter(models.LinkCode.user_id == user.id,
                                              models.LinkCode.used == False).all():  # noqa: E712
        c.used = True
    db.commit()
    S.audit(db, user, "unlink", "telegram", user.id, source="WEB")
    return {"ok": True, "connected": False}


@router.post("/link/verify")
def verify(body: LinkVerifyIn, db: Session = Depends(get_db)):
    rec = db.get(models.LinkCode, body.code.strip())
    now = datetime.now(timezone.utc)
    exp = _aware(rec.expires_at) if rec else None
    if not rec:
        raise HTTPException(400, "Invalid code")
    if rec.used:
        raise HTTPException(400, "This code was already used. Generate a new one.")
    if exp and exp < now:
        raise HTTPException(400, "This code has expired. Generate a new one.")
    rec.used = True  # one-time: burn immediately

    # Enforce one Telegram account per ERP user (edge case: multiple Telegram accounts /
    # re-linking): drop any prior link for this user before binding the new tg_id.
    for prior in db.query(models.TelegramLink).filter(models.TelegramLink.user_id == rec.user_id).all():
        if prior.tg_id != body.tg_id:
            db.delete(prior)
    db.merge(models.TelegramLink(tg_id=body.tg_id, user_id=rec.user_id, username=body.username,
                                 device=body.device, linked_at=now, last_activity=now,
                                 expires_at=now + timedelta(days=7)))
    db.commit()
    u = db.get(models.User, rec.user_id)
    S.audit(db, u, "link", "telegram", body.tg_id, detail=f"@{body.username}" if body.username else "",
            source="TELEGRAM")
    return {"ok": True, "user": {"id": u.id, "name": u.name, "role": u.role, "branches": u.branch_names or None}}


@router.get("/session/{tg_id}")
def session(tg_id: str, db: Session = Depends(get_db)):
    link = db.get(models.TelegramLink, tg_id)
    if not link:
        return {"linked": False}
    link.last_activity = datetime.now(timezone.utc)  # touch on every bot interaction
    db.commit()
    u = db.get(models.User, link.user_id)
    return {"linked": True, "user": {"id": u.id, "name": u.name, "role": u.role,
                                     "branches": u.branch_names or None}}
