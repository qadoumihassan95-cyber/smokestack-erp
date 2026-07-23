"""Wave B / B-B-C1 EXPAND — customers surrogate row_id + tenant-scoped business key.

Revision ID: u0j1k2l3m4n5
Revises: t9i0j1k2l3m4
Create Date: 2026-07-23

EXPAND phase for `customers` (first B-B canary). Purely additive + reversible:
adds a surrogate `row_id` (sequence-backed on PostgreSQL so new inserts populate
it automatically), backfills it for existing rows, backfills `company_id=1`, and
adds two unique indexes — `uq_customers_row_id (row_id)` and the tenant-scoped
`uq_customers_company_id (company_id, id)`. The legacy `id` primary key is left
intact so old code keeps working. On PostgreSQL both unique indexes are built
CONCURRENTLY (no table lock). Company #1's `C-01` row is untouched.
"""
from alembic import op
import sqlalchemy as sa

revision = "u0j1k2l3m4n5"
down_revision = "t9i0j1k2l3m4"
branch_labels = None
depends_on = None

SEQ = "customers_row_id_seq"
UQ_ROW = "uq_customers_row_id"
UQ_BIZ = "uq_customers_company_id"


def _cols(insp):
    return {c["name"] for c in insp.get_columns("customers")}


def _idx(insp):
    return {i["name"] for i in insp.get_indexes("customers")}


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "customers" not in insp.get_table_names():
        return
    pg = bind.dialect.name == "postgresql"

    # 1) surrogate column + backfill (transactional part)
    if "row_id" not in _cols(insp):
        if pg:
            op.execute(f"CREATE SEQUENCE IF NOT EXISTS {SEQ}")
            op.add_column("customers", sa.Column(
                "row_id", sa.BigInteger(),
                server_default=sa.text(f"nextval('{SEQ}')"), nullable=True))
            op.execute(f"ALTER SEQUENCE {SEQ} OWNED BY customers.row_id")
            # volatile default fills each existing row with a distinct value;
            # belt-and-suspenders for any that remained NULL
            op.execute(f"UPDATE customers SET row_id = nextval('{SEQ}') WHERE row_id IS NULL")
        else:
            with op.batch_alter_table("customers") as b:
                b.add_column(sa.Column("row_id", sa.Integer(), nullable=True))
            op.execute("UPDATE customers SET row_id = rowid WHERE row_id IS NULL")

    # 2) backfill tenant owner
    op.execute(sa.text("UPDATE customers SET company_id = 1 WHERE company_id IS NULL"))

    # 3) unique indexes (row_id surrogate + tenant-scoped business key)
    insp = sa.inspect(bind)
    have = _idx(insp)
    if pg:
        with op.get_context().autocommit_block():
            if UQ_ROW not in have:
                op.create_index(UQ_ROW, "customers", ["row_id"], unique=True,
                                postgresql_concurrently=True)
            if UQ_BIZ not in have:
                op.create_index(UQ_BIZ, "customers", ["company_id", "id"], unique=True,
                                postgresql_concurrently=True)
    else:
        if UQ_ROW not in have:
            op.create_index(UQ_ROW, "customers", ["row_id"], unique=True)
        if UQ_BIZ not in have:
            op.create_index(UQ_BIZ, "customers", ["company_id", "id"], unique=True)


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "customers" not in insp.get_table_names():
        return
    pg = bind.dialect.name == "postgresql"
    have = _idx(insp)
    if pg:
        with op.get_context().autocommit_block():
            if UQ_BIZ in have:
                op.drop_index(UQ_BIZ, table_name="customers", postgresql_concurrently=True)
            if UQ_ROW in have:
                op.drop_index(UQ_ROW, table_name="customers", postgresql_concurrently=True)
        if "row_id" in _cols(insp):
            op.drop_column("customers", "row_id")
        op.execute(f"DROP SEQUENCE IF EXISTS {SEQ}")
    else:
        if UQ_BIZ in have:
            op.drop_index(UQ_BIZ, table_name="customers")
        if UQ_ROW in have:
            op.drop_index(UQ_ROW, table_name="customers")
        if "row_id" in _cols(insp):
            with op.batch_alter_table("customers") as b:
                b.drop_column("row_id")
