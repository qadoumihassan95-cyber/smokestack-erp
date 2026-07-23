"""Engineering Phase 4 — per-company document counters."""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_ctr_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "ctr-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app import counters, tenancy

with TestClient(app):
    pass


def test_numbers_are_sequential_per_company():
    d1 = SessionLocal(); tenancy.set_session_company(d1, 9001)
    try:
        a = counters.next_number(d1, "PO"); b = counters.next_number(d1, "PO"); d1.commit()
        assert a == "PO-000001" and b == "PO-000002"
    finally:
        d1.close()


def test_counters_are_isolated_per_company_and_type():
    d1 = SessionLocal(); tenancy.set_session_company(d1, 9001)
    d2 = SessionLocal(); tenancy.set_session_company(d2, 9002)
    try:
        counters.next_number(d1, "TR"); d1.commit()   # company1 TR -> 1
        n2 = counters.next_number(d2, "TR"); d2.commit()  # company2 TR -> 1 (independent)
        assert n2 == "TR-000001"
        # different doc_type has its own sequence
        assert counters.next_number(d1, "AP") == "AP-000001"; d1.commit()
    finally:
        d1.close(); d2.close()


def test_no_duplicates_under_repeated_calls():
    d = SessionLocal(); tenancy.set_session_company(d, 9003)
    try:
        seen = {counters.next_number(d, "MV") for _ in range(200)}
        d.commit()
        assert len(seen) == 200   # all unique
    finally:
        d.close()


def test_full_alembic_chain_creates_counter_table():
    db = f"/tmp/ctr_chain_{os.getpid()}.db"
    if os.path.exists(db):
        os.remove(db)
    url = f"sqlite:///{db}"
    from app.config import settings
    settings.database_url = url
    os.environ["DATABASE_URL"] = url
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "migrations"))
    command.upgrade(cfg, "head")
    insp = sa.inspect(sa.create_engine(url))
    assert "document_counters" in insp.get_table_names()
    assert "idempotency_keys" in insp.get_table_names()
