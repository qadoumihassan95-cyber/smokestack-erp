"""telegram_links: add prefs (notification settings JSON)

Revision ID: c2d3e4f5a6b7
Revises: b7c1e2d3f4a5
Create Date: 2026-07-17
"""
from alembic import op
import sqlalchemy as sa

revision = "c2d3e4f5a6b7"
down_revision = "b7c1e2d3f4a5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("telegram_links") as batch:
        batch.add_column(sa.Column("prefs", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("telegram_links") as batch:
        batch.drop_column("prefs")
