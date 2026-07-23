"""Wave B / B-B-C5 EXPAND — approvals surrogate row_id + tenant-scoped key.
Revision ID: c8r9s0t1u2v3
Revises: b7q8r9s0t1u2
"""
import sqlalchemy as sa
from alembic import op
from app import bb_migrate
revision = "c8r9s0t1u2v3"; down_revision = "b7q8r9s0t1u2"; branch_labels = None; depends_on = None
_EXTRA = [sa.Column("kind", sa.String()), sa.Column("ref", sa.String()),
          sa.Column("branch", sa.String()), sa.Column("amount", sa.Numeric(12, 2)),
          sa.Column("requested_by", sa.String()), sa.Column("summary", sa.String()),
          sa.Column("status", sa.String()), sa.Column("decided_by", sa.String()),
          sa.Column("comment", sa.String()), sa.Column("created_at", sa.DateTime(timezone=True))]
def upgrade(): bb_migrate.expand(op, "approvals", _EXTRA)
def downgrade(): bb_migrate.expand_down(op, "approvals", _EXTRA)
