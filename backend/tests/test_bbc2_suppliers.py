"""Wave B / B-B-C2 — suppliers surrogate row_id + tenant-scoped business key."""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_bbc2_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "bbc2-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

import sqlalchemy as sa
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app import models, partners_repo, tenancy

with TestClient(app):
    pass


def _chain_engine():
    db = f"/tmp/bbc2_chain_{os.getpid()}.db"
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
    return sa.create_engine(url)


def test_chain_surrogate_pk_and_tenant_key():
    e = _chain_engine()
    insp = sa.inspect(e)
    assert set(insp.get_pk_constraint("suppliers").get("constrained_columns") or []) == {"row_id"}
    uqs = [set(u["column_names"]) for u in insp.get_unique_constraints("suppliers")]
    idx = [set(i["column_names"]) for i in insp.get_indexes("suppliers") if i.get("unique")]
    assert {"company_id", "id"} in uqs or {"company_id", "id"} in idx


def test_two_companies_same_supplier_number():
    e = _chain_engine()
    with e.begin() as c:
        c.execute(sa.text("INSERT INTO suppliers (company_id,id,name,balance) VALUES (1,'S-01','A',0)"))
        c.execute(sa.text("INSERT INTO suppliers (company_id,id,name,balance) VALUES (2,'S-01','B',0)"))
    with e.connect() as c:
        assert c.execute(sa.text("SELECT count(*) FROM suppliers WHERE id='S-01'")).scalar() == 2


def test_intra_company_duplicate_rejected():
    e = _chain_engine()
    with e.begin() as c:
        c.execute(sa.text("INSERT INTO suppliers (company_id,id,name) VALUES (1,'S-09','x')"))
    with pytest.raises(Exception):
        with e.begin() as c:
            c.execute(sa.text("INSERT INTO suppliers (company_id,id,name) VALUES (1,'S-09','y')"))


def test_scoped_repo_reads_and_isolates():
    d1 = SessionLocal(); tenancy.set_session_company(d1, 1)
    d2 = SessionLocal(); tenancy.set_session_company(d2, 2)
    try:
        s = partners_repo.get_supplier(d1, "S-01")
        assert s is not None and s.id == "S-01"
        assert partners_repo.get_supplier(d2, "S-01") is None
        assert partners_repo.list_suppliers(d2) == []
    finally:
        d1.close(); d2.close()
