"""Wave B / B-B-C4 CONTRACT — purchases primary key -> surrogate row_id.
Revision ID: b7q8r9s0t1u2
Revises: a6p7q8r9s0t1
"""
import sqlalchemy as sa
from alembic import op
from app import bb_migrate
revision = "b7q8r9s0t1u2"; down_revision = "a6p7q8r9s0t1"; branch_labels = None; depends_on = None
_EXTRA = [sa.Column("vendor", sa.String()), sa.Column("branch", sa.String()),
          sa.Column("amount", sa.Numeric(12, 2)), sa.Column("status", sa.String()),
          sa.Column("purchase_date", sa.Date())]
def upgrade(): bb_migrate.contract(op, "purchases", _EXTRA)
def downgrade(): bb_migrate.contract_down(op, "purchases", _EXTRA)
