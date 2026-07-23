"""Wave B / B-A2 CONTRACT — company_settings composite primary key.

Revision ID: t9i0j1k2l3m4
Revises: s8h9i0j1k2l3
Create Date: 2026-07-23

CONTRACT phase of the config-singleton canary. Moves the primary key from the
global `key` to the composite `(company_id, key)` and retires the now-redundant
legacy uniqueness. Preconditions verified in production before shipping this:
company_id backfilled (no NULLs), no duplicate (company_id, key), a single
company, and the composite unique index (B-A1) already live. Guarded + idempotent
+ reversible. On a single-row table the lock is momentary; the composite unique
already enforced correctness, so this only relabels which index backs the PK.
"""
from alembic import op
import sqlalchemy as sa

revision = "t9i0j1k2l3m4"
down_revision = "s8h9i0j1k2l3"
branch_labels = None
depends_on = None

UQ = "uq_company_settings_company_key"


def _pk_columns(insp):
    try:
        return set(insp.get_pk_constraint("company_settings").get("constrained_columns") or [])
    except Exception:
        return set()


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "company_settings" not in insp.get_table_names():
        return
    # already contracted? (idempotent)
    if _pk_columns(insp) == {"company_id", "key"}:
        return
    # safety: no NULL company_id may remain before it joins the PK
    op.execute(sa.text("UPDATE company_settings SET company_id = 1 WHERE company_id IS NULL"))

    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE company_settings ALTER COLUMN company_id SET NOT NULL")
        op.execute("ALTER TABLE company_settings ALTER COLUMN key SET NOT NULL")
        op.execute("ALTER TABLE company_settings DROP CONSTRAINT company_settings_pkey")
        op.execute("ALTER TABLE company_settings ADD CONSTRAINT company_settings_pkey "
                   "PRIMARY KEY (company_id, key)")
        # the standalone composite unique index is now redundant with the PK
        op.execute("DROP INDEX IF EXISTS " + UQ)
    else:
        # SQLite cannot ALTER a primary key in place; rebuild the table.
        op.execute("ALTER TABLE company_settings RENAME TO company_settings_old")
        op.create_table(
            "company_settings",
            sa.Column("company_id", sa.Integer, nullable=False, server_default="1"),
            sa.Column("key", sa.String, nullable=False),
            sa.Column("value", sa.Text),
            sa.Column("updated_by", sa.String),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("company_id", "key"),
        )
        op.execute("INSERT INTO company_settings (company_id, key, value, updated_by, updated_at) "
                   "SELECT company_id, key, value, updated_by, updated_at FROM company_settings_old")
        op.execute("DROP TABLE company_settings_old")


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "company_settings" not in insp.get_table_names():
        return
    # already expanded? (idempotent)
    if _pk_columns(insp) == {"key"}:
        return

    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE company_settings DROP CONSTRAINT company_settings_pkey")
        op.execute("ALTER TABLE company_settings ADD CONSTRAINT company_settings_pkey "
                   "PRIMARY KEY (key)")
        # restore the B-A1 composite unique index
        existing = {i["name"] for i in insp.get_indexes("company_settings")}
        if UQ not in existing:
            op.create_index(UQ, "company_settings", ["company_id", "key"], unique=True)
        op.execute("ALTER TABLE company_settings ALTER COLUMN company_id DROP NOT NULL")
    else:
        op.execute("ALTER TABLE company_settings RENAME TO company_settings_old")
        op.create_table(
            "company_settings",
            sa.Column("company_id", sa.Integer, nullable=True, server_default="1"),
            sa.Column("key", sa.String, nullable=False),
            sa.Column("value", sa.Text),
            sa.Column("updated_by", sa.String),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("key"),
        )
        op.execute("INSERT INTO company_settings (company_id, key, value, updated_by, updated_at) "
                   "SELECT company_id, key, value, updated_by, updated_at FROM company_settings_old")
        op.execute("DROP TABLE company_settings_old")
        op.create_index(UQ, "company_settings", ["company_id", "key"], unique=True)
