"""Wave B / B-A1 EXPAND — company_settings per-company key (additive only).

Revision ID: s8h9i0j1k2l3
Revises: r7g8h9i0j1k2
Create Date: 2026-07-23

EXPAND phase of the config-singleton canary. Purely additive and backward
compatible: adds the composite UNIQUE (company_id, key) that will become the PK
in the CONTRACT phase (B-A2). The existing PK on `key` is untouched, so old code
keeps working. Company #1's single 'business_timezone' row is unaffected.
Idempotent + reversible. On Postgres the index is created CONCURRENTLY (outside a
transaction) to avoid any table lock; on SQLite it is a normal index.
"""
from alembic import op
import sqlalchemy as sa

revision = "s8h9i0j1k2l3"
down_revision = "r7g8h9i0j1k2"
branch_labels = None
depends_on = None

IDX = "uq_company_settings_company_key"


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "company_settings" not in insp.get_table_names():
        return
    # make sure every row has a company_id before enforcing the composite unique
    op.execute(sa.text("UPDATE company_settings SET company_id = 1 WHERE company_id IS NULL"))
    existing = {i["name"] for i in insp.get_indexes("company_settings")}
    if IDX in existing:
        return
    if bind.dialect.name == "postgresql":
        # CONCURRENTLY cannot run inside a transaction block
        with op.get_context().autocommit_block():
            op.create_index(IDX, "company_settings", ["company_id", "key"],
                            unique=True, postgresql_concurrently=True)
    else:
        op.create_index(IDX, "company_settings", ["company_id", "key"], unique=True)


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "company_settings" not in insp.get_table_names():
        return
    existing = {i["name"] for i in insp.get_indexes("company_settings")}
    if IDX not in existing:
        return
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.drop_index(IDX, table_name="company_settings", postgresql_concurrently=True)
    else:
        op.drop_index(IDX, table_name="company_settings")
