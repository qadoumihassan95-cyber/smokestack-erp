"""PFS Control Center — a self-contained platform application.

Decoupled from the ERP by design: its own routing, auth realm, permissions,
service layer and data-access repository. It shares with the ERP ONLY the
database engine and schema (the shared-schema multi-tenancy model) — nothing
else. Co-hosted today via mount_pfs(); the exact same create_pfs_app() object
can be served as its own service / domain (e.g. pfs.mydomain.com) tomorrow with
no refactor.

To extract later:
  * point PFS_DATABASE_URL / PFS_JWT_SECRET at the new deployment,
  * run `uvicorn app.pfs.standalone:app` (or import create_pfs_app()) as root,
  * leave the ERP untouched (it never imports anything from this package).
"""
from .app import create_pfs_app
from .config import pfs_config


def mount_pfs(parent_app):
    """Compose the Control Center onto a parent ERP app (co-hosted mode).

    The ONLY line the ERP entrypoint needs. No-op when PFS_ENABLED is false.
    Returns the mounted sub-application (or None).
    """
    if not pfs_config.enabled:
        return None
    pfs_app = create_pfs_app()
    parent_app.mount(pfs_config.mount_path, pfs_app)

    # Run the Control Center's own self-seed from the parent startup, because a
    # mounted sub-app's lifespan is not reliably triggered by Starlette. The
    # bootstrap itself lives in this package (parent only invokes it).
    from .db import SessionLocal
    from .seed import bootstrap_super_admin

    @parent_app.on_event("startup")
    def _pfs_bootstrap():
        db = SessionLocal()
        try:
            bootstrap_super_admin(db)
        finally:
            db.close()

    return pfs_app


__all__ = ["mount_pfs", "create_pfs_app", "pfs_config"]
