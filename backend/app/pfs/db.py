"""Database access for the Control Center.

Shared-schema multi-tenancy: by default the Control Center reuses the ERP's
engine and session, so co-hosting adds no extra connection pool. Setting
PFS_DATABASE_URL makes it build its OWN engine — the single change needed when
the service is extracted to its own deployment. Either way, all real data access
goes through the repository (the seam), never straight from routes.
"""
from .config import pfs_config


def _normalize_db(url: str) -> str:
    # Render/Heroku hand out postgres:// ; SQLAlchemy 2 wants postgresql+psycopg2://
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    if url.startswith("postgresql://") and "+" not in url.split("://")[0]:
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


if pfs_config.database_url:
    # Standalone / extracted mode: own engine, small pool.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    _engine = create_engine(_normalize_db(pfs_config.database_url),
                            pool_pre_ping=True, pool_size=3, max_overflow=2)
    SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
else:
    # Co-hosted mode: reuse the shared engine (no second connection pool).
    from ..database import SessionLocal  # shared DB infrastructure


def get_pfs_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
