"""Wave B / B-B-C3 EXPAND — transfers surrogate row_id + tenant-scoped key.

Revision ID: y4n5o6p7q8r9
Revises: x3m4n5o6p7q8
Create Date: 2026-07-23
"""
import sqlalchemy as sa
from alembic import op
from app import bb_migrate

revision = "y4n5o6p7q8r9"
down_revision = "x3m4n5o6p7q8"
branch_labels = None
depends_on = None

_EXTRA = [sa.Column("sku", sa.String()), sa.Column("from_branch", sa.String()),
          sa.Column("to_branch", sa.String()), sa.Column("qty", sa.Integer()),
          sa.Column("status", sa.String())]


def upgrade():
    bb_migrate.expand(op, "transfers", _EXTRA)


def downgrade():
    bb_migrate.expand_down(op, "transfers", _EXTRA)
