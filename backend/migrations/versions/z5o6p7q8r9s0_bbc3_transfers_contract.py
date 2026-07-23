"""Wave B / B-B-C3 CONTRACT — transfers primary key -> surrogate row_id.
Revision ID: z5o6p7q8r9s0
Revises: y4n5o6p7q8r9
"""
import sqlalchemy as sa
from alembic import op
from app import bb_migrate
revision = "z5o6p7q8r9s0"; down_revision = "y4n5o6p7q8r9"; branch_labels = None; depends_on = None
_EXTRA = [sa.Column("sku", sa.String()), sa.Column("from_branch", sa.String()),
          sa.Column("to_branch", sa.String()), sa.Column("qty", sa.Integer()),
          sa.Column("status", sa.String())]
def upgrade(): bb_migrate.contract(op, "transfers", _EXTRA)
def downgrade(): bb_migrate.contract_down(op, "transfers", _EXTRA)
