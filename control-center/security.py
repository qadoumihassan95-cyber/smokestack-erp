"""PFS Control Center — operator authentication (its own realm) + platform audit.

Uses PyJWT (no jose/ecdsa/pyasn1 chain — clean from day one). The Control Plane realm is
entirely separate from any ERP customer realm (ADR-021).
"""
import datetime

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from sqlalchemy.orm import Session

import models
from config import settings
from database import get_db

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=True)


def hash_pw(p: str) -> str:
    return _pwd.hash(p[:72])


def verify_pw(p: str, h: str) -> bool:
    try:
        return _pwd.verify(p[:72], h)
    except Exception:
        return False


def create_token(operator_id: str) -> str:
    exp = datetime.datetime.utcnow() + datetime.timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode({"sub": operator_id, "exp": exp}, settings.jwt_secret, algorithm=settings.jwt_alg)


def current_operator(token: str = Depends(oauth2), db: Session = Depends(get_db)) -> "models.Operator":
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
        oid = payload.get("sub")
    except Exception:
        raise HTTPException(401, "Invalid or expired token")
    op = db.get(models.Operator, oid) if oid else None
    if not op or op.status != "active":
        raise HTTPException(401, "Unknown or inactive operator")
    return op


def audit(db, operator, action, target_type, target_id="", detail="", result="ok", commit=True):
    db.add(models.PlatformAuditLog(
        actor_operator_id=getattr(operator, "id", None), action=action,
        target_type=target_type, target_id=str(target_id), detail=detail, result=result))
    if commit:
        db.commit()
