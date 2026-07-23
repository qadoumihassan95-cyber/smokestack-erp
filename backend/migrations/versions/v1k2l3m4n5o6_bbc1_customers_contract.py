"""Wave B / B-B-C1 CONTRACT — customers primary key moves to surrogate row_id.

Revision ID: v1k2l3m4n5o6
Revises: u0j1k2l3m4n5
Create Date: 2026-07-23

CONTRACT phase for `customers`. Preconditions verified in production before
shipping: `row_id` present + unique (no NULLs), `company_id` backfilled, no
duplicate `(company_id, id)`, single company. Moves the primary key from the
legacy business `id` to the immutable surrogate `row_id` (promoting the existing
`uq_customers_row_id` index in place on PostgreSQL — no rebuild), enforces
`company_id`/`id` NOT NULL, and keeps the tenant-scoped `uq_customers_company_id`.
Idempotent + reversible. On a tiny table the metadata lock is momentary.
"""
from alembic import op
import sqlalchemy as sa

revision = "v1k2l3m4n5o6"
down_revision = "u0j1k2l3m4n5"
branch_labels = None
depends_on = None

UQ_ROW = "uq_customers_row_id"
UQ_BIZ = "uq_customers_company_id"


def _pk(insp):
    return set(insp.get_pk_constraint("customers").get("constrained_columns") or [])


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "customers" not in insp.get_table_names():
        return
    if _pk(insp) == {"row_id"}:
        return
    op.execute(sa.text("UPDATE customers SET company_id = 1 WHERE company_id IS NULL"))

    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE customers ALTER COLUMN row_id SET NOT NULL")
        op.execute("ALTER TABLE customers ALTER COLUMN company_id SET NOT NULL")
        op.execute("ALTER TABLE customers ALTER COLUMN id SET NOT NULL")
        op.execute("ALTER TABLE customers DROP CONSTRAINT customers_pkey")
        # promote the existing surrogate unique index to be the primary key
        op.execute(f"ALTER TABLE customers ADD CONSTRAINT customers_pkey "
                   f"PRIMARY KEY USING INDEX {UQ_ROW}")
    else:
        op.execute("ALTER TABLE customers RENAME TO customers_old")
        op.create_table(
            "customers",
            sa.Column("row_id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("company_id", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("name", sa.String()),
            sa.Column("balance", sa.Numeric(12, 2)),
            sa.UniqueConstraint("company_id", "id", name=UQ_BIZ),
        )
        op.execute("INSERT INTO customers (row_id, company_id, id, name, balance) "
                   "SELECT row_id, company_id, id, name, balance FROM customers_old")
        op.execute("DROP TABLE customers_old")


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "customers" not in insp.get_table_names():
        return
    if _pk(insp) == {"id"}:
        return

    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE customers DROP CONSTRAINT customers_pkey")
        op.execute(f"CREATE UNIQUE INDEX {UQ_ROW} ON customers (row_id)")
        op.execute("ALTER TABLE customers ADD CONSTRAINT customers_pkey PRIMARY KEY (id)")
        op.execute("ALTER TABLE customers ALTER COLUMN company_id DROP NOT NULL")
    else:
        op.execute("ALTER TABLE customers RENAME TO customers_old")
        op.create_table(
            "customers",
            sa.Column("company_id", sa.Integer(), nullable=True, server_default="1"),
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("name", sa.String()),
            sa.Column("balance", sa.Numeric(12, 2)),
            sa.Column("row_id", sa.Integer()),
        )
        op.execute("INSERT INTO customers (company_id, id, name, balance, row_id) "
                   "SELECT company_id, id, name, balance, row_id FROM customers_old")
        op.execute("DROP TABLE customers_old")
        op.create_index(UQ_ROW, "customers", ["row_id"], unique=True)
        op.create_index(UQ_BIZ, "customers", ["company_id", "id"], unique=True)
