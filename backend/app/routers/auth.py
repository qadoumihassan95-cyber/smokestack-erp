from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, security as S, ratelimit as RL

router = APIRouter(prefix="/api/auth", tags=["auth"])

def _user_dict(u: models.User):
    return {"id": u.id, "name": u.name, "role": u.role, "branches": u.branch_names or None, "email": u.email}

@router.post("/login")
def login(request: Request = None, form: OAuth2PasswordRequestForm = Depends(),
          db: Session = Depends(get_db)):
    # Throttle BEFORE verifying (SS-H-007): bounded attempts per client IP and per
    # supplied identifier. The 429 is identical whether or not the account exists,
    # so it never discloses which usernames are valid.
    RL.guard(db, request, form.username, kind="login", limit=10, window_sec=300)
    u = db.query(models.User).filter(models.User.id == form.username).first() \
        or db.query(models.User).filter(models.User.email == form.username).first()
    if not u or not S.verify_pw(form.password, u.password_hash):
        RL.note_failure(db, request, form.username, kind="login")
        S.audit(db, None, "failed_login", "user", form.username, result="denied")
        raise HTTPException(401, "Incorrect username or password")
    # identities provisioned for an employee's Telegram session carry RBAC only
    if getattr(u, "can_login", True) is False:
        S.audit(db, None, "failed_login", "user", form.username, result="denied")
        raise HTTPException(403, "This identity cannot sign in to the web app.")
    if u.status != "active":
        S.audit(db, None, "failed_login", "user", form.username, result="denied")
        raise HTTPException(403, "This account is not active.")
    # Policy layer 2 at login: block sign-in for suspended/archived/provisioning/
    # maintenance companies (active/trial/read-only allow login). No-op for Company #1.
    from .. import policy
    if not policy.can_login(db, getattr(u, "company_id", None)):
        S.audit(db, u, "failed_login", "user", u.id, result="denied")
        raise HTTPException(403, "This company is not currently available for sign-in.")
    S.audit(db, u, "login", "user", u.id)
    return {"access_token": S.make_token(u), "token_type": "bearer", "user": _user_dict(u),
            "must_change_password": bool(getattr(u, "must_change_password", False))}

@router.get("/me")
def me(user: models.User = Depends(S.get_current_user)):
    return _user_dict(user)


@router.post("/logout")
def logout(db: Session = Depends(get_db),
           user: models.User = Depends(S.get_current_user)):
    """Sign out. Because JWTs are stateless, we revoke by advancing the account's
    token version (SS-H-011) — every token issued before this call, including the
    one just used, stops validating."""
    S.bump_token_version(db, user)
    S.audit(db, user, "logout", "user", user.id)
    return {"ok": True}
