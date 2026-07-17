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
    seed_on_start: bool = _bool(os.getenv("SEED_ON_START"), True)
    # Password used for the demo accounts created by the seed. Overridable via
    # env so production can supply a real one without committing it to git.
    seed_password: str = os.getenv("SEED_PASSWORD", "demo1234")
    link_code_ttl_min: int = int(os.getenv("LINK_CODE_TTL_MIN", "10"))

settings = Settings()
