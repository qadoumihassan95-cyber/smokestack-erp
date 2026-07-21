"""User provisioning and account self-service.

Creating a login is a privileged, audited operation. Permissions themselves are
never written here — an account carries a ROLE, and every permission decision
comes from permissions.PERMS via security.require(). Adding a user therefore
cannot widen the permission model.
"""
import re
import secrets
import string
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models, security as S, permissions as P

router = APIRouter(prefix="/api", tags=["users"])

# Unambiguous alphabet: no O/0, l/1/I — these get transcribed by hand.
_PW_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
_PW_SYMBOLS = "!@#$%&*?"


def temp_password(length=14):
    """A strong one-time password the holder must replace at first login."""
    pool = _PW_ALPHABET + _PW_SYMBOLS
    while True:
        pw = "".join(secrets.choice(pool) for _ in range(length))
        # guarantee a mix so it survives any downstream complexity policy
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in _PW_SYMBOLS for c in pw)):
            return pw


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "", (text or "").strip().lower())


def unique_username(db, full_name):
    """first.last, de-duplicated with a numeric suffix. Never reuses a name."""
    parts = [p for p in re.split(r"\s+", (full_name or "").strip()) if p]
    first = _slug(parts[0]) if parts else "user"
    last = _slug(parts[-1]) if len(parts) > 1 else ""
    base = f"{first}.{last}" if last else first
    base = base[:40] or "user"
    candidate, n = base, 1
    while db.get(models.User, candidate):
        n += 1
        candidate = f"{base}{n}"
    return candidate


def _user_out(db, u, employee_id=None):
    return {"username": u.id, "name": u.name, "role": u.role,
            "branches": u.branch_names or [], "status": u.status,
            "can_login": bool(u.can_login),
            "must_change_password": bool(u.must_change_password),
            "employee_id": employee_id or u.employee_id,
            "created_at": str(u.created_at or "")}


@router.get("/users")
def list_users(db: Session = Depends(get_db),
               user: models.User = Depends(S.require("manage_users"))):
    rows = db.query(models.User).order_by(models.User.id).all()
    return [_user_out(db, u) for u in rows]


@router.post("/users", status_code=201)
def create_user(body: dict, db: Session = Depends(get_db),
                actor: models.User = Depends(S.require("manage_users"))):
    """Provision a login. Returns the temporary password EXACTLY ONCE."""
    name = (body or {}).get("name", "").strip()
    if not name:
        raise HTTPException(422, "A full name is required.")
    role = ((body or {}).get("role") or "employee").strip()
    if role not in P.PERMS:
        raise HTTPException(422, f"Unknown role: {role}. Known roles: "
                                 + ", ".join(sorted(P.PERMS)))
    branches = (body or {}).get("branches") or []
    if isinstance(branches, str):
        branches = [branches]
    known = set(S.all_branch_names(db))
    for b in branches:
        if b not in known:
            raise HTTPException(422, f"Unknown branch: {b}")
        # you cannot grant access to a branch you do not hold yourself
        S.assert_branch(actor, db, b)

    username = (body or {}).get("username") or unique_username(db, name)
    if db.get(models.User, username):
        raise HTTPException(409, f"Username already exists: {username}")

    pw = temp_password()
    u = models.User(id=username, name=name, role=role,
                    email=(body or {}).get("email"),
                    password_hash=S.hash_pw(pw), status="active",
                    can_login=True, must_change_password=True)
    db.add(u)
    db.flush()
    for b in branches:
        db.add(models.UserBranch(user_id=u.id, branch=b))

    # optionally mirror into the Employee register
    emp_id = None
    if (body or {}).get("create_employee", True):
        emp_id = (body or {}).get("employee_id") or f"EMP-{username.upper()}"
        if not db.get(models.Employee, emp_id):
            db.add(models.Employee(
                id=emp_id, name=name,
                branch=(branches[0] if branches else (sorted(known)[0] if known else None)),
                title=(body or {}).get("title") or role.replace("_", " ").title(),
                pay_type="salary", salary=0, active=True, role=role,
                user_id=u.id, created_by=actor.id))
            u.employee_id = emp_id
        else:
            emp_id = None            # never touch an existing employee record
    db.commit()

    S.audit(db, actor, "create_user", "user", username,
            detail=f"{name} · role={role} · branches={','.join(branches) or 'none'}")
    out = _user_out(db, u, emp_id)
    out["temp_password"] = pw        # shown once; never stored in plain text
    out["permissions"] = P.PERMS.get(role, [])
    return out


@router.post("/auth/change-password")
def change_password(body: dict, db: Session = Depends(get_db),
                    user: models.User = Depends(S.get_current_user)):
    """Any signed-in user replaces their own password, clearing the reset flag."""
    current = (body or {}).get("current_password") or ""
    new = (body or {}).get("new_password") or ""
    if not S.verify_pw(current, user.password_hash):
        raise HTTPException(403, "Your current password is not correct.")
    if len(new) < 10:
        raise HTTPException(422, "Choose a password of at least 10 characters.")
    if new == current:
        raise HTTPException(422, "The new password must be different.")
    user.password_hash = S.hash_pw(new)
    user.must_change_password = False
    db.commit()
    S.audit(db, user, "change_password", "user", user.id)
    return {"ok": True, "must_change_password": False}
