"""PFS Control Center configuration — independent of the ERP.

Read entirely from the environment so the Control Center can run either mounted
inside the ERP service (co-hosted, the default today) or as its OWN standalone
service / domain (e.g. pfs.mydomain.com) with no code change. When co-hosted,
sensible defaults fall back to the ERP's settings so nothing extra must be
configured to start.
"""
import os


def _bool(v, d=False):
    if v is None:
        return d
    return str(v).lower() in ("1", "true", "yes", "on")


class PFSConfig:
    # Mount the Control Center onto the ERP app (co-hosted mode). Set false to
    # run the ERP with the Control Center completely absent.
    enabled: bool = _bool(os.getenv("PFS_ENABLED"), True)
    # Path the sub-application is mounted at when co-hosted. Ignored in standalone
    # mode (the service is then served at "/").
    mount_path: str = os.getenv("PFS_MOUNT_PATH", "/pfs")
    # Its OWN auth realm. A distinct secret means an ERP token can never be used
    # against the Control Center and vice-versa. Falls back to JWT_SECRET when
    # co-hosted so no extra env is required to boot.
    jwt_secret: str = os.getenv("PFS_JWT_SECRET") or os.getenv(
        "JWT_SECRET", "dev-insecure-secret-change-me")
    jwt_alg: str = "HS256"
    jwt_realm: str = "pfs"                # stamped into every Control Center token
    jwt_expire_minutes: int = int(os.getenv("PFS_JWT_EXPIRE_MINUTES", "480"))
    # Its OWN database URL. Empty = reuse the shared platform DB engine (the
    # shared-schema multi-tenant model). Setting this is the single change needed
    # to point an extracted Control Center service at the database.
    database_url: str = os.getenv("PFS_DATABASE_URL", "")
    # First Super Admin, created idempotently on startup when BOTH are provided.
    # Never hardcoded — supplied via env only.
    root_user: str = os.getenv("PFS_ROOT_USER", "")
    root_password: str = os.getenv("PFS_ROOT_PASSWORD", "")


pfs_config = PFSConfig()
