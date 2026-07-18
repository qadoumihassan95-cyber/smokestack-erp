"""Financial Control Center: validation_runs audit-history table (additive only)

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-07-18
"""
from alembic import op
import sqlalchemy as sa

revision = "f5a6b7c8d9e0"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade():
    # Purely additive and idempotent: creates one new table used only to store
    # audit-history records. No existing table is touched.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "validation_runs" not in insp.get_table_names():
        op.create_table(
            "validation_runs",
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                      primary_key=True, autoincrement=True),
            sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("user_id", sa.String()),
            sa.Column("score", sa.Numeric(5, 2)),
            sa.Column("passed", sa.Integer()),
            sa.Column("warnings", sa.Integer()),
            sa.Column("errors", sa.Integer()),
            sa.Column("critical", sa.Integer()),
            sa.Column("duration_ms", sa.Integer()),
            sa.Column("modules", sa.String()),
            sa.Column("severity", sa.String()),
            sa.Column("report", sa.Text()),
        )
        op.create_index("ix_validation_runs_ts", "validation_runs", ["ts"])


def downgrade():
    op.drop_table("validation_runs")
