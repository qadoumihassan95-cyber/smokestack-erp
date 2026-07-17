"""branch geofence settings + attendance table

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-07-17
"""
from alembic import op
import sqlalchemy as sa

revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("branches") as b:
        b.add_column(sa.Column("lat", sa.Numeric(10, 6), nullable=True))
        b.add_column(sa.Column("lng", sa.Numeric(10, 6), nullable=True))
        b.add_column(sa.Column("radius_m", sa.Integer(), nullable=True))
        b.add_column(sa.Column("timezone", sa.String(), nullable=True))
        b.add_column(sa.Column("loc_verify", sa.Boolean(), nullable=True))
        b.add_column(sa.Column("grace_min", sa.Integer(), nullable=True))
        b.add_column(sa.Column("allow_override", sa.Boolean(), nullable=True))
        b.add_column(sa.Column("attendance_active", sa.Boolean(), nullable=True))
    op.create_table(
        "attendance",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(), index=True),
        sa.Column("employee_id", sa.String()),
        sa.Column("employee_name", sa.String()),
        sa.Column("tg_id", sa.String()),
        sa.Column("branch", sa.String(), index=True),
        sa.Column("clock_in_at", sa.DateTime(timezone=True)),
        sa.Column("ci_lat", sa.Numeric(10, 6)), sa.Column("ci_lng", sa.Numeric(10, 6)), sa.Column("ci_dist", sa.Integer()),
        sa.Column("clock_out_at", sa.DateTime(timezone=True)),
        sa.Column("co_lat", sa.Numeric(10, 6)), sa.Column("co_lng", sa.Numeric(10, 6)), sa.Column("co_dist", sa.Integer()),
        sa.Column("status", sa.String()),
        sa.Column("approval", sa.String()),
        sa.Column("approver", sa.String()), sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.String()),
        sa.Column("late", sa.Boolean()), sa.Column("worked_minutes", sa.Integer()),
        sa.Column("source", sa.String()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("attendance")
    with op.batch_alter_table("branches") as b:
        for c in ("attendance_active", "allow_override", "grace_min", "loc_verify",
                  "timezone", "radius_m", "lng", "lat"):
            b.drop_column(c)
