"""Wave B / B-A1 EXPAND — company_settings per-company key (expand phase).

Verifies the additive composite-unique + dual accessor are correct and backward
compatible: Company #1's timezone setting is read/written exactly as before, the
accessor is company-scoped, and the whole alembic chain still applies.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_ba1_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "ba1-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app import models, company_config, reports_tg, tenancy

with TestClient(app):
    pass


def test_composite_unique_exists_on_company_settings():
    insp = sa.inspect(SessionLocal().bind)
    uqs = insp.get_unique_constraints("company_settings")
    idx = insp.get_indexes("company_settings")
    covered = any(set(u["column_names"]) == {"company_id", "key"} for u in uqs) or \
              any(set(i["column_names"]) == {"company_id", "key"} and i.get("unique") for i in idx)
    assert covered, "composite (company_id, key) uniqueness must exist after expand"


def test_company_one_timezone_roundtrip_unchanged():
    # the existing report timezone accessor still works for Company #1
    db = SessionLocal()
    tenancy.set_session_company(db, 1)
    try:
        reports_tg.set_company_tz(db, "Asia/Hebron")
        assert reports_tg.company_tz(db) == "Asia/Hebron"
        # stored as a company-1 row
        row = company_config.get_setting(db, "business_timezone", company_id=1)
        assert row is not None and row.company_id == 1 and row.value == "Asia/Hebron"
    finally:
        db.close()


def test_accessor_read_scoping_in_expand_phase():
    # EXPAND phase: `key` is still the global PK, so two companies cannot share a
    # key yet (that is what B-A2 CONTRACT enables). But READ scoping already holds:
    # company 2 never sees company 1's setting. Distinct keys prove write scoping.
    d1 = SessionLocal(); tenancy.set_session_company(d1, 1)
    d2 = SessionLocal(); tenancy.set_session_company(d2, 2)
    try:
        company_config.set_value(d1, "ba1-greeting", "hello-1")
        assert company_config.get_value(d1, "ba1-greeting") == "hello-1"
        # company 2 does NOT see company 1's key
        assert company_config.get_value(d2, "ba1-greeting") is None
        # company 2 writes its own distinct key
        company_config.set_value(d2, "ba1-greeting-2", "hello-2")
        assert company_config.get_value(d2, "ba1-greeting-2") == "hello-2"
        assert company_config.get_value(d1, "ba1-greeting-2") is None
    finally:
        d1.close(); d2.close()


def test_full_alembic_chain_applies_with_ba1():
    db = f"/tmp/ba1_chain_{os.getpid()}.db"
    if os.path.exists(db):
        os.remove(db)
    url = f"sqlite:///{db}"
    from app.config import settings
    settings.database_url = url
    os.environ["DATABASE_URL"] = url
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location",
                        os.path.join(os.path.dirname(__file__), "..", "migrations"))
    command.upgrade(cfg, "head")
    insp = sa.inspect(sa.create_engine(url))
    assert "uq_company_settings_company_key" in {i["name"] for i in insp.get_indexes("company_settings")}
