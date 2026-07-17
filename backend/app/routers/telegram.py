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
import json
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from ..database import get_db
from ..config import settings
from .. import models, security as S
from ..schemas import LinkVerifyIn

router = APIRouter(prefix="/api/telegram", tags=["telegram"])

CODE_TTL_SECONDS = 300  # 5 minutes

DEFAULT_PREFS = {
    "daily_summary": True, "weekly_summary": True, "low_stock": True, "out_of_stock": True,
    "large_sales": False, "large_expenses": False, "quiet_hours": None,
    "language": "en", "default_branch": None, "timezone": "UTC",
    "att_consent": False,   # location-privacy consent for attendance
}


def _load_prefs(link):
    try:
        p = json.loads(link.prefs) if (link and link.prefs) else {}
    except Exception:  # noqa: BLE001
        p = {}
    return {**DEFAULT_PREFS, **(p or {})}


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
    """Resolve a Telegram id to its ERP user + link metadata (used by the bot's /me).
    Returns the full linked profile so the bot doesn't have to duplicate any logic."""
    link = db.get(models.TelegramLink, (tg_id or "").strip())
    if not link:
        return {"linked": False}
    u = db.get(models.User, link.user_id)
    if not u:
        # Orphaned link (user deleted): treat as unlinked rather than 500.
        return {"linked": False}
    link.last_activity = datetime.now(timezone.utc)  # touch on every bot interaction
    db.commit()
    return {"linked": True,
            "user": {"id": u.id, "name": u.name, "role": u.role, "branches": u.branch_names or None},
            "tg_id": link.tg_id, "username": link.username,
            "linked_at": _iso(link.linked_at), "last_activity": _iso(link.last_activity),
            "status": "connected"}


@router.post("/auth-token")
def auth_token(body: dict, x_bot_token: str = Header(None), db: Session = Depends(get_db)):
    """Exchange a linked Telegram id for that user's JWT. Only the bot (which knows
    the BotFather token, shared via the API's TELEGRAM_BOT_TOKEN env) may call this.
    The bot then reuses every existing RBAC-protected endpoint as the real user."""
    if not settings.bot_token or x_bot_token != settings.bot_token:
        raise HTTPException(403, "Forbidden")
    tg_id = (body.get("tg_id") or "").strip()
    link = db.get(models.TelegramLink, tg_id)
    if not link:
        raise HTTPException(404, "Not linked")
    u = db.get(models.User, link.user_id)
    if not u or u.status != "active":
        raise HTTPException(403, "User is not active")
    link.last_activity = datetime.now(timezone.utc)
    db.commit()
    return {"access_token": S.make_token(u), "token_type": "bearer",
            "user": {"id": u.id, "name": u.name, "role": u.role, "branches": u.branch_names or None},
            "prefs": _load_prefs(link)}


@router.post("/audit")
def bot_audit(body: dict, x_bot_token: str = Header(None), db: Session = Depends(get_db)):
    """Telegram-attributed audit entry (captures tg_id + old/new values, source=TELEGRAM).
    Complements the per-endpoint ERP-user audit written by the reused write endpoints."""
    if not settings.bot_token or x_bot_token != settings.bot_token:
        raise HTTPException(403, "Forbidden")
    db.add(models.AuditLog(source="TELEGRAM", tg_id=str(body.get("tg_id") or ""),
                           user_id=body.get("user_id"), action=body.get("action"),
                           entity=body.get("entity"), ref=str(body.get("ref") or ""),
                           detail=str(body.get("detail") or ""), result=body.get("result") or "ok"))
    db.commit()
    return {"ok": True}


@router.get("/prefs")
def get_prefs(db: Session = Depends(get_db), user: models.User = Depends(S.get_current_user)):
    link = db.query(models.TelegramLink).filter(models.TelegramLink.user_id == user.id).first()
    return {"connected": bool(link), "prefs": _load_prefs(link)}


@router.put("/prefs")
def put_prefs(body: dict, db: Session = Depends(get_db), user: models.User = Depends(S.get_current_user)):
    link = db.query(models.TelegramLink).filter(models.TelegramLink.user_id == user.id).first()
    if not link:
        raise HTTPException(404, "No Telegram link for this account")
    cur = _load_prefs(link)
    for k, v in (body or {}).items():
        if k in DEFAULT_PREFS:
            cur[k] = v
    link.prefs = json.dumps(cur)
    db.commit()
    S.audit(db, user, "update_prefs", "telegram", link.tg_id)
    return {"prefs": cur}
