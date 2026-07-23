"""Phase 1 M2 — additive company_id migration: backfill + idempotency.

Verifies the tenantization migration is backward compatible: existing rows
created BEFORE company_id existed are backfilled to Company #1, the whole alembic
chain applies cleanly (this is what Render runs on preDeploy), and re-running the
tenant upgrade is a no-op (idempotent), so a retried deploy is safe.
"""
import os

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations

_BACKEND = os.path.join(os.path.dirname(__file__), "..")

PRE = "p5e6f7g8h9i0"      # revision before tenantization
TENANT = "q6f7g8h9i0j1"   # the tenantization revision


def _cfg(url):
    # alembic env.py reads app.config.settings.database_url at migration time, so
    # mutate it here to target this test's DB (env is evaluated per command run).
    os.environ["DATABASE_URL"] = url
    from app.config import settings
    settings.database_url = url
    c = Config(os.path.join(_BACKEND, "alembic.ini"))
    c.set_main_option("script_location", os.path.join(_BACKEND, "migrations"))
    return c


def test_backfill_assigns_existing_rows_to_company_one():
    db = f"/tmp/pf_backfill_{os.getpid()}.db"
    if os.path.exists(db):
        os.remove(db)
    url = f"sqlite:///{db}"
    cfg = _cfg(url)
    command.upgrade(cfg, PRE)                       # schema WITHOUT company_id
    eng = sa.create_engine(url)
    with eng.begin() as cx:                         # legacy rows, no company_id
        cx.execute(sa.text("INSERT INTO ledger (branch, type, amount) "
                            "VALUES ('Store A', 'sale', 10)"))
        cx.execute(sa.text("INSERT INTO products (sku, name) VALUES ('X1', 'Widget')"))
    command.upgrade(cfg, TENANT)                    # tenantize + backfill
    with eng.connect() as cx:
        led = cx.execute(sa.text("SELECT company_id FROM ledger")).fetchall()
        prod = cx.execute(sa.text("SELECT company_id FROM products")).fetchall()
    assert led and all(r[0] == 1 for r in led)
    assert prod and all(r[0] == 1 for r in prod)


def test_full_chain_applies_and_platform_tables_are_not_tenantized():
    db = f"/tmp/pf_chain_{os.getpid()}.db"
    if os.path.exists(db):
        os.remove(db)
    url = f"sqlite:///{db}"
    command.upgrade(_cfg(url), "head")
    insp = sa.inspect(sa.create_engine(url))
    # tenant tables gained company_id
    for t in ("ledger", "products", "attendance", "chat_rooms", "company_settings"):
        assert "company_id" in {c["name"] for c in insp.get_columns(t)}
    # the platform 'companies' table is NOT a tenant table
    assert "company_id" not in {c["name"] for c in insp.get_columns("companies")}


def test_tenant_upgrade_is_idempotent():
    db = f"/tmp/pf_idem_{os.getpid()}.db"
    if os.path.exists(db):
        os.remove(db)
    url = f"sqlite:///{db}"
    cfg = _cfg(url)
    command.upgrade(cfg, "head")
    import importlib
    rev = importlib.import_module("migrations.versions.q6f7g8h9i0j1_tenant_company_id")
    eng = sa.create_engine(url)
    # running the tenant upgrade body again must not raise (guards skip existing)
    with eng.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            rev.upgrade()
            rev.upgrade()
    with eng.connect() as cx:
        n = cx.execute(sa.text("SELECT count(*) FROM ledger")).scalar()
    assert n is not None
