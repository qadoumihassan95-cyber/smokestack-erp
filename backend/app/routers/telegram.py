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
from .. import models, security as S, permissions as P, tg_caps as C
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


def _identity_for_employee(db: Session, emp: models.Employee) -> models.User:
    """Return the login identity an employee's Telegram session acts as,
    provisioning one on first link.

    Employees are not users: only seven seeded logins exist and there is no user
    management UI. Without this, an owner could only ever link accounts that map
    back to his OWN user — which is precisely why every new link replaced the
    previous one. The provisioned identity carries the employee's role and
    branch, and cannot sign in to the web app.
    """
    if emp.user_id:
        u = db.get(models.User, emp.user_id)
        if u:
            return u
    # back-compat: an employee whose name matches a real login keeps that login
    u = db.query(models.User).filter(models.User.name == emp.name).first()
    if not u:
        uid = f"EMP-{emp.id}"
        u = db.get(models.User, uid)
    if not u:
        u = models.User(
            id=f"EMP-{emp.id}", name=emp.name,
            role=(emp.role or "employee"),
            email=None,
            # deliberately unusable: this identity exists only for Telegram RBAC
            password_hash=S.hash_pw(secrets.token_urlsafe(24)),
            status="active", can_login=False, employee_id=emp.id)
        db.add(u); db.flush()
        if emp.branch:
            db.add(models.UserBranch(user_id=u.id, branch=emp.branch))
    if u.employee_id != emp.id:
        u.employee_id = emp.id
    emp.user_id = u.id
    db.flush()
    return u


def _issue_code(db: Session, user: models.User, employee: models.Employee = None):
    """Mint a one-time invitation.

    Scoped to the TARGET EMPLOYEE, not to the signed-in operator: only that
    employee's own outstanding codes are invalidated, so an owner can prepare
    invitations for several people without cancelling each other.
    """
    if employee is not None:
        identity = _identity_for_employee(db, employee)
        emp_id = employee.id
        stale = db.query(models.LinkCode).filter(
            models.LinkCode.employee_id == emp_id,
            models.LinkCode.used == False).all()  # noqa: E712
    else:
        identity = user
        emp = db.query(models.Employee).filter(models.Employee.name == user.name).first()
        emp_id = emp.id if emp else None
        stale = db.query(models.LinkCode).filter(
            models.LinkCode.user_id == user.id,
            models.LinkCode.employee_id == None,          # noqa: E711
            models.LinkCode.used == False).all()           # noqa: E712
    for old in stale:
        old.used = True
    code = f"{secrets.randbelow(1000000):06d}"
    expires = datetime.now(timezone.utc) + timedelta(seconds=CODE_TTL_SECONDS)
    db.add(models.LinkCode(code=code, user_id=identity.id, expires_at=expires,
                           used=False, employee_id=emp_id, created_by=user.id))
    db.commit()
    S.audit(db, user, "issue_link_code", "telegram", code,
            detail=f"for {employee.name}" if employee is not None else "self")
    return {"code": code, "expires_at": _iso(expires), "expires_in": CODE_TTL_SECONDS,
            "employee_id": emp_id,
            "employee": (employee.name if employee is not None else user.name)}


def _resolve_target(db: Session, actor: models.User, employee_id: str):
    """Owners/managers may mint an invitation for any employee they administer."""
    if not employee_id:
        return None
    if "manage_users" not in P.PERMS.get(actor.role, []):
        raise HTTPException(403, "You may not link Telegram accounts for other employees.")
    emp = db.get(models.Employee, employee_id)
    if not emp:
        raise HTTPException(404, "Employee not found")
    if not emp.active:
        raise HTTPException(422, "That employee is not active.")
    S.assert_branch(actor, db, emp.branch)
    return emp


@router.post("/link-code")
def link_code(body: dict = None, db: Session = Depends(get_db),
              user: models.User = Depends(S.get_current_user)):
    emp = _resolve_target(db, user, (body or {}).get("employee_id"))
    return _issue_code(db, user, emp)


@router.post("/link/issue")
def issue(body: dict = None, db: Session = Depends(get_db),
          user: models.User = Depends(S.get_current_user)):
    # Legacy shape kept working; also returns ttl_minutes for old callers.
    emp = _resolve_target(db, user, (body or {}).get("employee_id"))
    r = _issue_code(db, user, emp)
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

    # ---- INSERT-ONLY LINKING -------------------------------------------------
    # Linking must never modify or delete another row. The previous
    # implementation deleted every prior link belonging to the code's user, and
    # because every code carried the SIGNED-IN OWNER, each new link wiped the
    # one before it. Now we validate and insert; nothing else is touched.

    # (a) a Telegram account is globally unique — it may represent one employee
    taken = db.get(models.TelegramLink, body.tg_id)
    if taken:
        raise HTTPException(409, "This Telegram account is already linked. "
                                 "Remove it from the Telegram Management Center first.")

    # (b) resolve the employee this invitation was minted for
    emp = db.get(models.Employee, rec.employee_id) if rec.employee_id else None
    identity = db.get(models.User, rec.user_id)
    if emp is None and identity is not None:
        emp = db.query(models.Employee).filter(models.Employee.name == identity.name).first()
    if identity is None and emp is not None:
        identity = _identity_for_employee(db, emp)
    if identity is None:
        raise HTTPException(400, "This code is no longer valid.")

    # (c) one ACTIVE Telegram account per employee — and, when a link has no
    #     employee mapping, per session identity, so nobody silently accumulates
    #     devices. We reject rather than replace: existing rows are never touched.
    if emp is not None:
        clash = (db.query(models.TelegramLink)
                 .filter(models.TelegramLink.employee_id == emp.id,
                         models.TelegramLink.status == "active").first())
        who = emp.name
    else:
        clash = (db.query(models.TelegramLink)
                 .filter(models.TelegramLink.user_id == identity.id,
                         models.TelegramLink.status == "active").first())
        who = identity.name
    if clash:
        raise HTTPException(409, f"{who} already has an active Telegram account "
                                 f"(@{clash.username or clash.tg_id}). Disable or remove "
                                 f"it before linking a new one.")

    db.add(models.TelegramLink(
        tg_id=body.tg_id, user_id=identity.id, username=body.username,
        device=body.device, linked_at=now, last_activity=now,
        expires_at=now + timedelta(days=7), status="active",
        employee_id=(emp.id if emp is not None else None),
        linked_by=rec.created_by or rec.user_id))
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


def _overrides(emp):
    if not emp or not emp.tg_perms:
        return {}
    try:
        v = json.loads(emp.tg_perms)
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _caps_for_link(db, link):
    """Effective capabilities for a linked Telegram account, derived from the
    employee's ERP role via the shared permission engine."""
    u = db.get(models.User, link.user_id)
    emp = _emp_for(db, link, u)
    role = (u.role if u else (emp.role if emp else "employee"))
    return C.effective(role, _overrides(emp), P), emp, u, role


@router.get("/capabilities/{tg_id}")
def capabilities(tg_id: str, x_bot_token: str = Header(None), db: Session = Depends(get_db)):
    """What may this Telegram account do? Used by the bot to build its menu."""
    if not settings.bot_token or x_bot_token != settings.bot_token:
        raise HTTPException(403, "Forbidden")
    link = db.get(models.TelegramLink, (tg_id or "").strip())
    if not link:
        raise HTTPException(404, "Not linked")
    if (link.status or "active") != "active":
        raise HTTPException(403, "This Telegram account is disabled")
    caps, emp, u, role = _caps_for_link(db, link)
    return {"tg_id": link.tg_id, "employee": (emp.name if emp else (u.name if u else None)),
            "employee_id": (emp.id if emp else None), "role": role,
            "branches": (u.branch_names if u else []) or ([emp.branch] if emp and emp.branch else []),
            "capabilities": caps,
            "labels": C.CAP_LABEL}


@router.post("/authorize")
def authorize(body: dict, x_bot_token: str = Header(None), db: Session = Depends(get_db)):
    """THE permission gate. The bot calls this before executing any command.

    Answers three questions with one shared engine: is the account active, does
    the employee's ERP role (plus the owner's toggles) allow this capability,
    and is the requested branch inside the employee's scope. Every call — allowed
    or denied — is written to the audit log.
    """
    if not settings.bot_token or x_bot_token != settings.bot_token:
        raise HTTPException(403, "Forbidden")
    tg_id = str(body.get("tg_id") or "").strip()
    cap = str(body.get("capability") or "").strip()
    branch = body.get("branch") or None
    command = body.get("command") or cap

    link = db.get(models.TelegramLink, tg_id)
    if not link or (link.status or "active") != "active":
        _deny_audit(db, None, tg_id, command, branch, None,
                    "account not linked or disabled")
        return {"allowed": False, "reason": "not_linked", "message": C.DENIED_MESSAGE}

    caps, emp, u, role = _caps_for_link(db, link)
    scope = (u.branch_names if u else None) or ([emp.branch] if emp and emp.branch else [])
    if u and P.can_see_all(u.role) and not scope:
        scope = S.all_branch_names(db)

    if cap not in C.CAP_KEYS:
        reason = "unknown_capability"
        allowed = False
    elif not caps.get(cap):
        reason = ("disabled_by_owner" if C.role_allows(role, cap, P) else "role_forbids")
        allowed = False
    elif branch and scope and branch not in scope and not (u and P.can_see_all(u.role)):
        reason = "branch_out_of_scope"
        allowed = False
    else:
        reason = ""
        allowed = True

    link.last_activity = datetime.now(timezone.utc)
    db.add(models.AuditLog(
        source="TELEGRAM", tg_id=tg_id, user_id=(u.id if u else None),
        action=command, entity="telegram_command",
        ref=cap, detail=(C.CAP_LABEL.get(cap, cap) + (f" @ {branch}" if branch else "")),
        result=("ok" if allowed else "denied"),
        tg_username=link.username, branch=(branch or (scope[0] if len(scope) == 1 else None)),
        role=role, ip="telegram"))
    db.commit()

    out = {"allowed": allowed, "capability": cap, "employee": (emp.name if emp else None),
           "role": role, "branches": scope}
    if not allowed:
        out["reason"] = reason
        out["message"] = C.DENIED_MESSAGE
    return out


def _deny_audit(db, u, tg_id, command, branch, role, detail):
    db.add(models.AuditLog(source="TELEGRAM", tg_id=tg_id,
                           user_id=(u.id if u else None), action=command,
                           entity="telegram_command", detail=detail, result="denied",
                           branch=branch, role=role, ip="telegram"))
    db.commit()


@router.get("/link-code/{code}/status")
def link_code_status(code: str, db: Session = Depends(get_db),
                     user: models.User = Depends(S.get_current_user)):
    """Has this invitation been redeemed yet?

    The linking panel polls this instead of the signed-in user's own status,
    because an owner normally links SOMEBODY ELSE — watching his own account
    would never report the employee's connection.
    """
    rec = db.get(models.LinkCode, (code or "").strip())
    if not rec:
        raise HTTPException(404, "Unknown code")
    emp = db.get(models.Employee, rec.employee_id) if rec.employee_id else None
    out = {"code": rec.code, "used": bool(rec.used), "linked": False,
           "employee": (emp.name if emp else None),
           "employee_id": rec.employee_id,
           "expires_at": _iso(rec.expires_at),
           "expired": bool(rec.expires_at and _aware(rec.expires_at) < datetime.now(timezone.utc))}
    if rec.used:
        q = db.query(models.TelegramLink)
        link = (q.filter(models.TelegramLink.employee_id == rec.employee_id,
                         models.TelegramLink.status == "active").first()
                if rec.employee_id else
                q.filter(models.TelegramLink.user_id == rec.user_id,
                         models.TelegramLink.status == "active")
                 .order_by(models.TelegramLink.linked_at.desc()).first())
        if link:
            out["linked"] = True
            out["account"] = _account_row(db, link)
    return out


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
