"""User provisioning: force password change on first login

Revision ID: l1a2b3c4d5e6
Revises: k0f1a2b3c4d5
Create Date: 2026-07-21

Adds users.must_change_password. Existing accounts are explicitly backfilled to
FALSE so nobody currently signed in is suddenly locked behind a password reset.
Only accounts created through /api/users are flagged TRUE.
"""
from alembic import op
import sqlalchemy as sa

revision = "l1a2b3c4d5e6"
down_revision = "k0f1a2b3c4d5"
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    have = {c["name"] for c in insp.get_columns("users")}
    if "must_change_password" not in have:
        with op.batch_alter_table("users") as b:
            b.add_column(sa.Column("must_change_password", sa.Boolean(), nullable=True))
    # never force a reset on an account that already exists
    op.execute(sa.text("UPDATE users SET must_change_password = :v "
                       "WHERE must_change_password IS NULL").bindparams(v=False))


def downgrade():
    with op.batch_alter_table("users") as b:
        b.drop_column("must_change_password")
