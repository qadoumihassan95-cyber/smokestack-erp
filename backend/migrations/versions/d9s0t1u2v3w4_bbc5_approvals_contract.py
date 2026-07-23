"""Wave B / B-B-C5 CONTRACT — approvals primary key -> surrogate row_id.
Revision ID: d9s0t1u2v3w4
Revises: c8r9s0t1u2v3
"""
import sqlalchemy as sa
from alembic import op
from app import bb_migrate
revision = "d9s0t1u2v3w4"; down_revision = "c8r9s0t1u2v3"; branch_labels = None; depends_on = None
_EXTRA = [sa.Column("kind", sa.String()), sa.Column("ref", sa.String()),
          sa.Column("branch", sa.String()), sa.Column("amount", sa.Numeric(12, 2)),
          sa.Column("requested_by", sa.String()), sa.Column("summary", sa.String()),
          sa.Column("status", sa.String()), sa.Column("decided_by", sa.String()),
          sa.Column("comment", sa.String()), sa.Column("created_at", sa.DateTime(timezone=True))]
def upgrade(): bb_migrate.contract(op, "approvals", _EXTRA)
def downgrade(): bb_migrate.contract_down(op, "approvals", _EXTRA)
