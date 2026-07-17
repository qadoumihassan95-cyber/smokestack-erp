"""Auth + RBAC: password hashing, JWT, current-user dependency, permission +
branch guards, and an audit helper. Every protected route depends on these."""
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from . import models, permissions as P

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

def hash_pw(p: str) -> str:
    return pwd.hash(p)

def verify_pw(p: str, h: str) -> bool:
    try:
        return pwd.verify(p, h)
    except Exception:
        return False

def make_token(user: models.User) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode({"sub": user.id, "role": user.role, "exp": exp}, settings.jwt_secret, algorithm=settings.jwt_alg)

def get_current_user(token: str = Depends(oauth2), db: Session = Depends(get_db)) -> models.User:
    cred = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token", {"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
        uid = payload.get("sub")
    except JWTError:
        raise cred
    user = db.get(models.User, uid)
    if not user or user.status != "active":
        raise cred
    return user

def require(*perms):
    """Dependency factory: 403 unless the user has ALL listed permissions."""
    def _dep(user: models.User = Depends(get_current_user)):
        for p in perms:
            if not P.can(user.role, p):
                raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing permission: {p}")
        return user
    return _dep

def all_branch_names(db: Session):
    return [b.name for b in db.query(models.Branch).order_by(models.Branch.name).all()]

def scope_branches(user: models.User, db: Session):
    return P.allowed_branches(user, all_branch_names(db))

def assert_branch(user: models.User, db: Session, branch: str):
    if branch not in scope_branches(user, db):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Branch not permitted: {branch}")

def audit(db: Session, user, action, entity, ref="", detail="", source="WEB", result="ok"):
    db.add(models.AuditLog(source=source, user_id=getattr(user, "id", None),
                           action=action, entity=entity, ref=str(ref), detail=detail, result=result))
    db.commit()
