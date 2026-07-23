"""Wave B / B-B-C2 CONTRACT — suppliers primary key moves to surrogate row_id.

Revision ID: x3m4n5o6p7q8
Revises: w2l3m4n5o6p7
Create Date: 2026-07-23
"""
import sqlalchemy as sa
from alembic import op
from app import bb_migrate

revision = "x3m4n5o6p7q8"
down_revision = "w2l3m4n5o6p7"
branch_labels = None
depends_on = None

_EXTRA = [sa.Column("name", sa.String()), sa.Column("balance", sa.Numeric(12, 2))]


def upgrade():
    bb_migrate.contract(op, "suppliers", _EXTRA)


def downgrade():
    bb_migrate.contract_down(op, "suppliers", _EXTRA)
