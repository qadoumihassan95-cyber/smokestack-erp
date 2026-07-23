"""Idempotent bootstrap of the first Super Admin — from the environment ONLY.

Never hardcodes a credential. No-op unless BOTH PFS_ROOT_USER and
PFS_ROOT_PASSWORD are set, and only creates the account if it does not exist.
Owned by the Control Center so it self-provisions in either deployment mode.
"""
from . import security
from .config import pfs_config
from .repository import PlatformRepository


def bootstrap_super_admin(db):
    if not (pfs_config.root_user and pfs_config.root_password):
        return None
    repo = PlatformRepository(db)
    if repo.get_super_admin_by_username(pfs_config.root_user):
        return None
    return repo.create_super_admin(
        id="SA-root", username=pfs_config.root_user, name="Platform Root",
        password_hash=security.hash_pw(pfs_config.root_password))
