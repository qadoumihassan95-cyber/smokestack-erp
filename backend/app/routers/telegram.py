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
from .. import models, security as S, permissions as P
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
    link = (db.query(models.TelegramLink)
            .filter(models.TelegramLink.user_id == user_id)
            .order_by(models.TelegramLink.linked_at.desc()).first())
    if not link:
        return {"connected": False}
    if (link.status or "active") != "active":
        return {"connected": False, "disabled": True, "tg_id": link.tg_id,
                "username": link.username}
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

    # A Telegram id may only ever represent one employee.
    taken = db.get(models.TelegramLink, body.tg_id)
    if taken and taken.user_id != rec.user_id:
        raise HTTPException(409, "This Telegram account is already linked to another employee.")

    # One active Telegram account per employee: replace only THIS employee's own
    # previous account. Accounts belonging to every other employee are untouched,
    # so linking a new person never disconnects anyone else.
    prior_rows = db.query(models.TelegramLink).filter(
        models.TelegramLink.user_id == rec.user_id).all()
    keep_prefs, keep_emp = None, None
    for prior in prior_rows:
        if prior.tg_id != body.tg_id:
            # carry the employee mapping + preferences across a device change
            keep_prefs = keep_prefs or prior.prefs
            keep_emp = keep_emp or prior.employee_id
            db.delete(prior)

    u0 = db.get(models.User, rec.user_id)
    emp = None
    if u0:
        emp = db.query(models.Employee).filter(models.Employee.name == u0.name).first()
    existing = db.get(models.TelegramLink, body.tg_id)
    db.merge(models.TelegramLink(
        tg_id=body.tg_id, user_id=rec.user_id, username=body.username,
        device=body.device,
        linked_at=(existing.linked_at if existing and existing.linked_at else now),
        last_activity=now, expires_at=now + timedelta(days=7),
        status="active",
        employee_id=(keep_emp or (existing.employee_id if existing else None) or (emp.id if emp else None)),
        linked_by=rec.user_id,
        prefs=(existing.prefs if existing and existing.prefs else keep_prefs)))
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
    if (link.status or "active") != "active":
        return {"linked": False, "disabled": True,
                "message": "This Telegram account has been disabled by an administrator."}
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
    if (link.status or "active") != "active":
        raise HTTPException(403, "This Telegram account is disabled")
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
                           detail=str(body.get("detail") or ""), result=body.get("result") or "ok",
                           tg_username=body.get("tg_username"), branch=body.get("branch"),
                           role=body.get("role"), ip=body.get("ip")))
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


# ==========================================================================
# TELEGRAM MANAGEMENT CENTER — company-wide multi-account administration.
# Additive: none of the endpoints above changed behaviour for existing links.
# ==========================================================================
def _emp_for(db, link, u):
    """Resolve the Employee this Telegram account represents."""
    e = None
    if link.employee_id:
        e = db.get(models.Employee, link.employee_id)
    if not e and u:
        e = db.query(models.Employee).filter(models.Employee.name == u.name).first()
    return e


def _account_row(db, link):
    u = db.get(models.User, link.user_id)
    e = _emp_for(db, link, u)
    branches = (u.branch_names if u else None) or []
    if not branches and e and e.branch:
        branches = [e.branch]
    return {
        "tg_id": link.tg_id,
        "username": link.username,
        "user_id": link.user_id,
        "employee_id": (e.id if e else link.employee_id),
        "employee": (e.name if e else (u.name if u else "—")),
        "role": (u.role if u else None),
        "branches": branches,
        "branch": (", ".join(branches) if branches else "All branches"),
        "permissions": (P.PERMS.get(u.role, []) if u else []),
        "linked_at": _iso(link.linked_at),
        "last_activity": _iso(link.last_activity),
        "status": (link.status or "active"),
        "device": link.device,
        "linked_by": link.linked_by,
        "disabled_at": _iso(link.disabled_at),
        "disabled_by": link.disabled_by,
    }


@router.get("/accounts")
def accounts(q: str = "", branch: str = "all", role: str = "all", status: str = "all",
             employee: str = "all", db: Session = Depends(get_db),
             user: models.User = Depends(S.require("view_all_branches"))):
    """Every Telegram account linked to the company, with search + filters."""
    rows = [_account_row(db, l) for l in
            db.query(models.TelegramLink).order_by(models.TelegramLink.linked_at.desc()).all()]
    ql = (q or "").strip().lower()
    out = []
    for r in rows:
        if status != "all" and r["status"] != status:
            continue
        if role != "all" and (r["role"] or "") != role:
            continue
        if employee != "all" and (r["employee"] or "") != employee:
            continue
        if branch != "all" and branch not in (r["branches"] or []):
            continue
        if ql and ql not in " ".join([str(r.get("employee") or ""), str(r.get("username") or ""),
                                      str(r.get("tg_id") or ""), str(r.get("role") or ""),
                                      str(r.get("branch") or "")]).lower():
            continue
        out.append(r)
    return out


@router.get("/stats")
def tg_stats(db: Session = Depends(get_db),
             user: models.User = Depends(S.require("view_all_branches"))):
    links = db.query(models.TelegramLink).all()
    active = sum(1 for l in links if (l.status or "active") == "active")
    last_sync = max([_aware(l.last_activity) for l in links if l.last_activity] or [None]) \
        if links else None
    last_cmd = (db.query(models.AuditLog).filter(models.AuditLog.source == "TELEGRAM")
                .order_by(models.AuditLog.ts.desc()).first())
    return {"total": len(links), "active": active, "disabled": len(links) - active,
            "last_sync": _iso(last_sync) if last_sync else None,
            "last_bot_activity": _iso(last_cmd.ts) if last_cmd else None,
            "last_bot_action": (last_cmd.action if last_cmd else None),
            "bot_configured": bool(settings.bot_token)}


def _find_link(db, tg_id):
    link = db.get(models.TelegramLink, (tg_id or "").strip())
    if not link:
        raise HTTPException(404, "Telegram account not found")
    return link


@router.post("/accounts/{tg_id}/disable")
def disable_account(tg_id: str, db: Session = Depends(get_db),
                    user: models.User = Depends(S.require("manage_users"))):
    """Disable one account. Every other linked account keeps working."""
    link = _find_link(db, tg_id)
    link.status = "disabled"
    link.disabled_at = datetime.now(timezone.utc)
    link.disabled_by = user.id
    db.commit()
    S.audit(db, user, "disable", "telegram_account", tg_id,
            detail=f"@{link.username or ''}", source="WEB")
    return _account_row(db, link)


@router.post("/accounts/{tg_id}/enable")
def enable_account(tg_id: str, db: Session = Depends(get_db),
                   user: models.User = Depends(S.require("manage_users"))):
    link = _find_link(db, tg_id)
    link.status = "active"
    link.disabled_at = None
    link.disabled_by = None
    db.commit()
    S.audit(db, user, "enable", "telegram_account", tg_id,
            detail=f"@{link.username or ''}", source="WEB")
    return _account_row(db, link)


@router.delete("/accounts/{tg_id}")
def remove_account(tg_id: str, db: Session = Depends(get_db),
                   user: models.User = Depends(S.require("manage_users"))):
    """Remove a single account. Other accounts are never touched."""
    link = _find_link(db, tg_id)
    uname = link.username
    db.delete(link)
    db.commit()
    S.audit(db, user, "remove", "telegram_account", tg_id,
            detail=f"@{uname or ''}", source="WEB")
    return {"ok": True, "removed": tg_id}


@router.get("/accounts/{tg_id}/activity")
def account_activity(tg_id: str, limit: int = 100, db: Session = Depends(get_db),
                     user: models.User = Depends(S.require("view_all_branches"))):
    """Full audit trail for one Telegram account."""
    link = _find_link(db, tg_id)
    rows = (db.query(models.AuditLog).filter(models.AuditLog.tg_id == str(tg_id))
            .order_by(models.AuditLog.ts.desc()).limit(min(limit, 500)).all())
    return {"account": _account_row(db, link),
            "entries": [{"ts": _iso(a.ts), "action": a.action, "entity": a.entity,
                         "ref": a.ref, "detail": a.detail, "result": a.result,
                         "user": a.user_id, "tg_username": a.tg_username,
                         "branch": a.branch, "role": a.role, "ip": a.ip}
                        for a in rows]}
