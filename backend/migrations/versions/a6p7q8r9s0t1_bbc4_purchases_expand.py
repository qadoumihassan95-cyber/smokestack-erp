"""Wave B / B-B-C4 EXPAND — purchases surrogate row_id + tenant-scoped key.
Revision ID: a6p7q8r9s0t1
Revises: z5o6p7q8r9s0
"""
import sqlalchemy as sa
from alembic import op
from app import bb_migrate
revision = "a6p7q8r9s0t1"; down_revision = "z5o6p7q8r9s0"; branch_labels = None; depends_on = None
_EXTRA = [sa.Column("vendor", sa.String()), sa.Column("branch", sa.String()),
          sa.Column("amount", sa.Numeric(12, 2)), sa.Column("status", sa.String()),
          sa.Column("purchase_date", sa.Date())]
def upgrade(): bb_migrate.expand(op, "purchases", _EXTRA)
def downgrade(): bb_migrate.expand_down(op, "purchases", _EXTRA)
