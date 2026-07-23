"""Wave B / B-B-C2 EXPAND — suppliers surrogate row_id + tenant-scoped key.

Revision ID: w2l3m4n5o6p7
Revises: v1k2l3m4n5o6
Create Date: 2026-07-23
"""
import sqlalchemy as sa
from alembic import op
from app import bb_migrate

revision = "w2l3m4n5o6p7"
down_revision = "v1k2l3m4n5o6"
branch_labels = None
depends_on = None

_EXTRA = [sa.Column("name", sa.String()), sa.Column("balance", sa.Numeric(12, 2))]


def upgrade():
    bb_migrate.expand(op, "suppliers", _EXTRA)


def downgrade():
    bb_migrate.expand_down(op, "suppliers", _EXTRA)
