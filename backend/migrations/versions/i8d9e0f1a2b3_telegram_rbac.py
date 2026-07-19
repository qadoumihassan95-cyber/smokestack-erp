"""Telegram RBAC: per-employee capability overrides

Revision ID: i8d9e0f1a2b3
Revises: h7c8d9e0f1a2
Create Date: 2026-07-19

One additive column. employees.tg_perms holds a JSON object of
{capability_key: bool} recording the owner's explicit switches.

NULL / absent means "follow the ERP role", so every existing employee keeps
exactly the capabilities their role already implies — this migration changes
nobody's access on the way in.
"""
from alembic import op
import sqlalchemy as sa

revision = "i8d9e0f1a2b3"
down_revision = "h7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    have = {c["name"] for c in insp.get_columns("employees")}
    if "tg_perms" not in have:
        with op.batch_alter_table("employees") as b:
            b.add_column(sa.Column("tg_perms", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("employees") as b:
        b.drop_column("tg_perms")
