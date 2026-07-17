"""v3: expense custom_description, employee schedule, licenses table

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-07-18
"""
from alembic import op
import sqlalchemy as sa

revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade():
    # Idempotent — add only what's missing so a re-run after an interrupted
    # deploy completes cleanly and stamps the revision.
    bind = op.get_bind()
    insp = sa.inspect(bind)

    ledger_cols = {c["name"] for c in insp.get_columns("ledger")}
    if "custom_description" not in ledger_cols:
        with op.batch_alter_table("ledger") as b:
            b.add_column(sa.Column("custom_description", sa.String(), nullable=True))

    emp_cols = {c["name"] for c in insp.get_columns("employees")}
    add = [(n, sa.String()) for n in ("sched_start", "sched_end", "sched_days") if n not in emp_cols]
    if add:
        with op.batch_alter_table("employees") as b:
            for n, t in add:
                b.add_column(sa.Column(n, t, nullable=True))

    if "licenses" not in insp.get_table_names():
        op.create_table(
            "licenses",
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("doc_type", sa.String()),
            sa.Column("branch", sa.String()),
            sa.Column("doc_number", sa.String()),
            sa.Column("authority", sa.String()),
            sa.Column("issue_date", sa.Date()),
            sa.Column("expiry_date", sa.Date()),
            sa.Column("status", sa.String()),
            sa.Column("responsible", sa.String()),
            sa.Column("notes", sa.Text()),
            sa.Column("attachment", sa.String()),
            sa.Column("created_by", sa.String()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_licenses_branch", "licenses", ["branch"])
        op.create_index("ix_licenses_expiry_date", "licenses", ["expiry_date"])


def downgrade():
    op.drop_table("licenses")
    with op.batch_alter_table("employees") as b:
        for c in ("sched_days", "sched_end", "sched_start"):
            b.drop_column(c)
    with op.batch_alter_table("ledger") as b:
        b.drop_column("custom_description")
