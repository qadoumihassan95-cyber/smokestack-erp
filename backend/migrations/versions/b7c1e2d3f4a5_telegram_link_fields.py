"""telegram_links: add username + last_activity

Revision ID: b7c1e2d3f4a5
Revises: 2bb7261b70dc
Create Date: 2026-07-17

Adds the columns the Settings -> Telegram linking UI displays (Telegram username
and last-activity timestamp). Existing rows get NULLs, which the app treats as
"unknown / just linked".
"""
from alembic import op
import sqlalchemy as sa

revision = "b7c1e2d3f4a5"
down_revision = "2bb7261b70dc"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("telegram_links") as batch:
        batch.add_column(sa.Column("username", sa.String(), nullable=True))
        batch.add_column(sa.Column("last_activity", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    with op.batch_alter_table("telegram_links") as batch:
        batch.drop_column("last_activity")
        batch.drop_column("username")
