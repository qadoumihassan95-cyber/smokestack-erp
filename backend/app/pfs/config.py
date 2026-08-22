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


    def secret_problems(self, erp_jwt_secret, default_secret):
        """Insecure signing-secret configurations for THIS realm (SEC-12).

        Returns a list of human-readable problems, empty when the configuration is
        sound. The ERP's secret and the known development default arrive as ARGUMENTS:
        the Control Center must not import ERP configuration (it is decoupled so it can
        be extracted), and the composition root is the right place to hold both halves.

        The finding: `jwt_secret` above falls back to `JWT_SECRET` so a co-hosted
        deployment needs no extra environment. That makes this module's own claim —
        that tokens "never cross between the tenant app and the Control Center" —
        false against anyone holding the ERP signing key: they can mint a token with
        `realm="pfs"` and be a platform super admin over every tenant. The realm check
        is separation only against honestly-issued tokens.

        Deriving this secret from the ERP one would not fix it — the derivation would
        be in the source, so holding `JWT_SECRET` still yields this key. The only fix
        is that they are genuinely different values, which is a deployment decision;
        this reports when that decision has not been made.
        """
        if not self.enabled:
            return []
        if not self.jwt_secret or self.jwt_secret == default_secret:
            return ["PFS_JWT_SECRET is unset or the insecure development default."]
        if erp_jwt_secret and self.jwt_secret == erp_jwt_secret:
            return ["PFS_JWT_SECRET is the same value as JWT_SECRET. The Control Center "
                    "is a separate authentication realm; sharing the signing key means "
                    "anyone holding the ERP secret can mint a platform super-admin token."]
        return []


pfs_config = PFSConfig()
