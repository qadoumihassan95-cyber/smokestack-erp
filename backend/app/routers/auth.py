from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, security as S

router = APIRouter(prefix="/api/auth", tags=["auth"])

def _user_dict(u: models.User):
    return {"id": u.id, "name": u.name, "role": u.role, "branches": u.branch_names or None, "email": u.email}

@router.post("/login")
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    u = db.query(models.User).filter(models.User.id == form.username).first() \
        or db.query(models.User).filter(models.User.email == form.username).first()
    if not u or not S.verify_pw(form.password, u.password_hash):
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
