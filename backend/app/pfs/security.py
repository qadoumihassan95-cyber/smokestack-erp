"""Control Center auth realm — deliberately separate from the ERP's security.

Own password hashing and own JWT (stamped with realm="pfs") so credentials and
tokens never cross between the tenant app and the Control Center. An ERP token
presented here is rejected, and a Control Center token is meaningless to the ERP.
"""
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .config import pfs_config
from .db import get_pfs_db
from .repository import PlatformRepository

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
# tokenUrl is relative to the mounted sub-app root (docs convenience only).
_oauth2 = OAuth2PasswordBearer(tokenUrl="auth/login")

BCRYPT_MAX_BYTES = 72


def hash_pw(p: str) -> str:
    if len(p.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise ValueError("Password too long: bcrypt supports at most 72 bytes.")
    return _pwd.hash(p)


def verify_pw(p: str, h: str) -> bool:
    if len(p.encode("utf-8")) > BCRYPT_MAX_BYTES:
        return False
    try:
        return _pwd.verify(p, h)
    except Exception:
        return False


def make_token(user) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=pfs_config.jwt_expire_minutes)
    return jwt.encode({"sub": user.id, "realm": pfs_config.jwt_realm, "exp": exp},
                      pfs_config.jwt_secret, algorithm=pfs_config.jwt_alg)


def require_super_admin(token: str = Depends(_oauth2),
                        db: Session = Depends(get_pfs_db)):
    cred = HTTPException(status.HTTP_401_UNAUTHORIZED,
                         "Invalid or expired Control Center token",
                         {"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, pfs_config.jwt_secret,
                             algorithms=[pfs_config.jwt_alg])
    except JWTError:
        raise cred
    # Reject anything not minted for this realm (e.g. an ERP tenant token).
    if payload.get("realm") != pfs_config.jwt_realm:
        raise cred
    user = PlatformRepository(db).get_super_admin(payload.get("sub"))
    if not user or not user.active:
        raise cred
    return user
