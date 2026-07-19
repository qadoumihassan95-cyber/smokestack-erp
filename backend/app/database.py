"""SQLAlchemy engine + session. Uses DATABASE_URL (PostgreSQL) in production,
falls back to a local SQLite file for dev/tests so the suite runs anywhere."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from .config import settings

url = settings.database_url
_is_sqlite = url.startswith("sqlite")
# SQLite serialises writers with a file lock. Without an explicit busy timeout a
# writer that collides with another connection can wait indefinitely, which is
# how the dev/test suite could hang. Production runs PostgreSQL and is unaffected.
connect_args = ({"check_same_thread": False, "timeout": 15} if _is_sqlite else {})
# pool tuned for many concurrent branches/records
engine = create_engine(
    url, connect_args=connect_args,
    pool_pre_ping=True, pool_size=10, max_overflow=20, future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
