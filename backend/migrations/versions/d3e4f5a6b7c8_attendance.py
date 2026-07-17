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
    # Idempotent: a prior interrupted deploy may have partially applied this
    # migration (created the attendance table and/or some branch columns)
    # without stamping the version. Add only what's missing so a re-run
    # completes cleanly and alembic can record the revision.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_cols = {c["name"] for c in insp.get_columns("branches")}
    new_cols = [
        ("lat", sa.Numeric(10, 6)), ("lng", sa.Numeric(10, 6)),
        ("radius_m", sa.Integer()), ("timezone", sa.String()),
        ("loc_verify", sa.Boolean()), ("grace_min", sa.Integer()),
        ("allow_override", sa.Boolean()), ("attendance_active", sa.Boolean()),
    ]
    to_add = [sa.Column(n, t, nullable=True) for (n, t) in new_cols if n not in existing_cols]
    if to_add:
        with op.batch_alter_table("branches") as b:
            for col in to_add:
                b.add_column(col)
    if "attendance" not in insp.get_table_names():
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
