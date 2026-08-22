"""Central configuration read from environment."""
import os

def _bool(v, d=False):
    if v is None:
        return d
    return str(v).lower() in ("1", "true", "yes", "on")

def _normalize_db(url: str) -> str:
    # Render/Heroku hand out postgres:// ; SQLAlchemy 2 wants postgresql+psycopg2://
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    if url.startswith("postgresql://") and "+" not in url.split("://")[0]:
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url

class Settings:
    database_url: str = _normalize_db(os.getenv("DATABASE_URL", "") or "sqlite:///./smokestack.db")
    jwt_secret: str = os.getenv("JWT_SECRET", "dev-insecure-secret-change-me")
    jwt_alg: str = "HS256"
    jwt_expire_minutes: int = int(os.getenv("JWT_EXPIRE_MINUTES", "720"))
    cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
    # SECURITY (SEC HIGH-02): how many reverse proxies sit in front of this app.
    # 0 (the default) means "none" — the client address is the socket peer and the
    # X-Forwarded-For header is IGNORED, because it is entirely caller-supplied and
    # rotating it defeats every per-IP throttle. Set to 1 behind exactly one trusted
    # proxy (Render's router): the rightmost entry is then the one THAT proxy wrote,
    # which an attacker cannot forge — extra entries they prepend are simply skipped.
    # Never raise this above the number of proxies you actually control.
    trusted_proxy_hops: int = max(0, int(os.getenv("TRUSTED_PROXY_HOPS", "0") or 0))
    # SECURITY (SEC CRITICAL-01): how long a stored Idempotency-Key response may be
    # replayed. Bounds both the table and the window in which a cached body can be
    # returned after the caller's access has changed.
    idempotency_ttl_hours: int = max(1, int(os.getenv("IDEMPOTENCY_TTL_HOURS", "24") or 24))
    # Demo/default-account seeding. SECURITY: this must NEVER default-on in
    # production. The default is derived from the backend store: local SQLite
    # (dev/tests) defaults to True so `uvicorn app.main:app` and the test client
    # get demo fixtures with zero config; a real database (PostgreSQL in prod)
    # defaults to False, so an unset/mis-set env var can never recreate the
    # default U-* accounts on a production boot. An explicit env value always wins.
    seed_on_start: bool = _bool(os.getenv("SEED_ON_START"), database_url.startswith("sqlite"))
    # Password used for the demo accounts created by the seed. Overridable via
    # env so production can supply a real one without committing it to git.
    seed_password: str = os.getenv("SEED_PASSWORD", "demo1234")
    # SIM-08: fabricated OPERATIONAL/FINANCIAL fixtures — sample products, stock,
    # back-dated movements, customer/supplier balances, licences and ledger money.
    # Defaults to False EVERYWHERE, including SQLite: a clean install must be
    # operationally empty, and invented revenue reaching a real dashboard is worse
    # than an empty one. Demo data is an explicit action, never a side effect.
    # (Structure — branches and role logins — is still created, so the app is usable.)
    seed_demo_data: bool = _bool(os.getenv("SEED_DEMO_DATA"), False)
    link_code_ttl_min: int = int(os.getenv("LINK_CODE_TTL_MIN", "10"))
    # Shared secret between the API and the Telegram worker. When set (== the
    # BotFather token), the worker can exchange a linked tg_id for that user's JWT.
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    # --- Team Chat image attachments (durable Postgres storage) ---
    chat_attach_max_bytes: int = int(os.getenv("CHAT_ATTACH_MAX_BYTES", str(5 * 1024 * 1024)))  # 5 MB
    chat_attach_max_dim: int = int(os.getenv("CHAT_ATTACH_MAX_DIM", "4096"))                     # px per side
    chat_thumb_dim: int = int(os.getenv("CHAT_THUMB_DIM", "320"))                                # thumbnail box
    # --- Telegram attendance evidence (location + selfie) ---
    att_attempt_ttl_min: int = int(os.getenv("ATT_ATTEMPT_TTL_MIN", "10"))     # attempt expiry (short)
    att_selfie_retention_days: int = int(os.getenv("ATT_SELFIE_RETENTION_DAYS", "90"))  # deletion
    att_selfie_max_bytes: int = int(os.getenv("ATT_SELFIE_MAX_BYTES", str(8 * 1024 * 1024)))  # 8 MB
    # SEC-15: selfies are now decoded and re-encoded at ingest, so a per-side pixel
    # bound is needed for the same reason chat has one — a decompression bomb is a
    # small file until it is opened.
    att_selfie_max_dim: int = int(os.getenv("ATT_SELFIE_MAX_DIM", "4096"))       # px per side

    # Values that must never reach a real (non-SQLite) production database.
    _DEFAULT_JWT_SECRET = "dev-insecure-secret-change-me"
    _DEFAULT_SEED_PASSWORD = "demo1234"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def production_secret_problems(self):
        """List the dangerous default secrets present on a production store (SS-C-003).
        Empty on SQLite (dev/tests). Used by startup to fail closed rather than boot a
        production service with guessable credentials."""
        if self.is_sqlite:
            return []
        problems = []
        if not self.jwt_secret or self.jwt_secret == self._DEFAULT_JWT_SECRET:
            problems.append("JWT_SECRET is unset or the insecure development default.")
        if self.seed_on_start and self.seed_password == self._DEFAULT_SEED_PASSWORD:
            problems.append("SEED_ON_START is enabled with the default SEED_PASSWORD on a "
                            "production database — this would create default-credential accounts.")
        # SECURITY (SEC-12) is checked by the COMPOSITION ROOT, not here. The rule
        # belongs to the Control Center realm and needs this secret to compare
        # against, but `app/config.py` may not import `app/pfs` — the sub-app is
        # decoupled so it can be extracted, and `tests/test_pfs_decoupling.py`
        # enforces that. `pfs_config.secret_problems()` states the rule and `main.py`
        # supplies both halves. (A first draft imported pfs here and the decoupling
        # test caught it, which is the test doing exactly its job.)
        return problems

settings = Settings()
