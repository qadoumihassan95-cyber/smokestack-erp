"""Control Center application logic.

Depends only on the repository (the data seam) and the local auth/permission
modules — never on ERP internals. This is where Control Center behaviour lives
as features are added (companies, lifecycle, subscriptions, features, audit …).
"""
from . import permissions, security
from .repository import PlatformRepository


def _role(user):
    # PlatformUser has no role column yet; every Super Admin is omnipotent.
    return getattr(user, "role", None) or "super_admin"


def authenticate(db, username, password):
    repo = PlatformRepository(db)
    u = repo.get_super_admin_by_username(username)
    if not u or not u.active or not security.verify_pw(password, u.password_hash or ""):
        return None
    repo.touch_login(u)
    return u


def me(user):
    role = _role(user)
    return {
        "id": user.id,
        "username": user.username,
        "name": user.name,
        "role": role,
        "capabilities": sorted(permissions.capabilities_for(role)),
        "must_change_password": bool(getattr(user, "must_change_password", False)),
    }


def overview(db):
    repo = PlatformRepository(db)
    return {
        "companies": repo.count_companies(),
        "active_companies": repo.count_companies("active"),
    }
