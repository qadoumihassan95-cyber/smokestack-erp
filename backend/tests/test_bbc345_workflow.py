"""Wave B / B-B-C3..C5 — transfers, purchases, approvals surrogate row_id +
tenant-scoped business key. Schema-level proof via the full alembic chain."""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_bbc345_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "bbc345-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

import sqlalchemy as sa
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.main import app

with TestClient(app):
    pass

_PFX = {"transfers": "TR", "purchases": "PO", "approvals": "AP"}


def _chain_engine():
    db = f"/tmp/bbc345_chain_{os.getpid()}.db"
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


@pytest.mark.parametrize("table", ["transfers", "purchases", "approvals"])
def test_surrogate_pk_and_tenant_key(table):
    e = _chain_engine()
    insp = sa.inspect(e)
    assert set(insp.get_pk_constraint(table).get("constrained_columns") or []) == {"row_id"}
    uqs = [set(u["column_names"]) for u in insp.get_unique_constraints(table)]
    idx = [set(i["column_names"]) for i in insp.get_indexes(table) if i.get("unique")]
    assert {"company_id", "id"} in uqs or {"company_id", "id"} in idx


@pytest.mark.parametrize("table", ["transfers", "purchases", "approvals"])
def test_two_companies_same_number_and_intra_dup_rejected(table):
    e = _chain_engine()
    pfx = _PFX[table]
    with e.begin() as c:
        c.execute(sa.text(f"INSERT INTO {table} (company_id,id) VALUES (1,'{pfx}-01')"))
        c.execute(sa.text(f"INSERT INTO {table} (company_id,id) VALUES (2,'{pfx}-01')"))
    with e.connect() as c:
        assert c.execute(sa.text(f"SELECT count(*) FROM {table} WHERE id='{pfx}-01'")).scalar() == 2
    with pytest.raises(Exception):
        with e.begin() as c:
            c.execute(sa.text(f"INSERT INTO {table} (company_id,id) VALUES (1,'{pfx}-01')"))
