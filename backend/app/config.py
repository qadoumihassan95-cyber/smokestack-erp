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
    link_code_ttl_min: int = int(os.getenv("LINK_CODE_TTL_MIN", "10"))
    # Shared secret between the API and the Telegram worker. When set (== the
    # BotFather token), the worker can exchange a linked tg_id for that user's JWT.
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

settings = Settings()
