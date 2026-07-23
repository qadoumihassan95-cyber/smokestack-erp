"""Wave B / B-B-C1 — customers surrogate row_id + tenant-scoped business key.

Two layers:
  * schema layer — run the full alembic chain (which reaches CONTRACT) on a fresh
    SQLite DB and prove the contracted rules directly: PK is `row_id`, the
    tenant-scoped `(company_id, id)` is unique, TWO companies may hold the SAME
    business number `C-01`, and an intra-company duplicate is rejected.
  * app layer — the scoped repo resolves a customer by (company_id, id) and is
    tenant-isolated. These pass in BOTH schema phases (repo never uses the PK).
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_bbc1_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "bbc1-secret-long-enough"
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
    db = f"/tmp/bbc1_chain_{os.getpid()}.db"
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


# --------------------------------------------------------------- schema layer
def test_chain_lands_on_surrogate_pk_and_tenant_business_key():
    e = _chain_engine()
    insp = sa.inspect(e)
    pk = set(insp.get_pk_constraint("customers").get("constrained_columns") or [])
    assert pk == {"row_id"}, f"expected surrogate PK, got {pk}"
    uqs = [set(u["column_names"]) for u in insp.get_unique_constraints("customers")]
    idx = [set(i["column_names"]) for i in insp.get_indexes("customers") if i.get("unique")]
    assert {"company_id", "id"} in uqs or {"company_id", "id"} in idx, \
        "tenant-scoped (company_id, id) uniqueness must exist"


def test_two_companies_may_share_the_same_business_number():
    e = _chain_engine()
    with e.begin() as c:
        c.execute(sa.text("INSERT INTO customers (company_id, id, name, balance) "
                          "VALUES (1,'C-01','Acme #1',0)"))
        c.execute(sa.text("INSERT INTO customers (company_id, id, name, balance) "
                          "VALUES (2,'C-01','Beta #2',0)"))
    with e.connect() as c:
        n = c.execute(sa.text("SELECT count(*) FROM customers WHERE id='C-01'")).scalar()
    assert n == 2, "two companies must each be able to hold business number C-01"


def test_intra_company_duplicate_business_number_rejected():
    e = _chain_engine()
    with e.begin() as c:
        c.execute(sa.text("INSERT INTO customers (company_id, id, name) VALUES (1,'C-09','x')"))
    with pytest.raises(Exception):
        with e.begin() as c:
            c.execute(sa.text("INSERT INTO customers (company_id, id, name) VALUES (1,'C-09','y')"))


# ------------------------------------------------------------------ app layer
def test_scoped_repo_reads_company_one_customer():
    db = SessionLocal(); tenancy.set_session_company(db, 1)
    try:
        c = partners_repo.get_customer(db, "C-01")
        assert c is not None and c.id == "C-01"
        assert any(x.id == "C-01" for x in partners_repo.list_customers(db))
    finally:
        db.close()


def test_scoped_repo_is_tenant_isolated():
    # company 2 must not see company 1's C-01
    db = SessionLocal(); tenancy.set_session_company(db, 2)
    try:
        assert partners_repo.get_customer(db, "C-01") is None
        assert partners_repo.list_customers(db) == []
    finally:
        db.close()
