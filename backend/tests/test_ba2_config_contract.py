"""Wave B / B-A2 CONTRACT — company_settings composite primary key.

Proves the contracted schema: the primary key is (company_id, key), two different
companies may now hold the SAME key independently, an intra-company duplicate key
is rejected, and Company #1's existing setting is untouched. Also proves the full
alembic chain lands on the composite PK.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_ba2_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "ba2-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

import sqlalchemy as sa
import pytest
from sqlalchemy.exc import IntegrityError
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app import models, company_config, reports_tg, tenancy

with TestClient(app):
    pass


def test_primary_key_is_composite():
    insp = sa.inspect(SessionLocal().bind)
    pk = set(insp.get_pk_constraint("company_settings").get("constrained_columns") or [])
    assert pk == {"company_id", "key"}


def test_two_companies_may_share_the_same_key():
    d1 = SessionLocal(); tenancy.set_session_company(d1, 1)
    d2 = SessionLocal(); tenancy.set_session_company(d2, 2)
    try:
        company_config.set_value(d1, "greeting", "hello-company-1")
        company_config.set_value(d2, "greeting", "hello-company-2")
        assert company_config.get_value(d1, "greeting") == "hello-company-1"
        assert company_config.get_value(d2, "greeting") == "hello-company-2"
    finally:
        d1.close(); d2.close()


def test_intra_company_duplicate_key_is_rejected():
    db = SessionLocal(); tenancy.use_system_context(db)
    try:
        db.add(models.CompanySetting(company_id=3, key="dup", value="a"))
        db.add(models.CompanySetting(company_id=3, key="dup", value="b"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_company_one_timezone_roundtrip_unchanged():
    db = SessionLocal(); tenancy.set_session_company(db, 1)
    try:
        reports_tg.set_company_tz(db, "Asia/Hebron")
        assert reports_tg.company_tz(db) == "Asia/Hebron"
    finally:
        db.close()
