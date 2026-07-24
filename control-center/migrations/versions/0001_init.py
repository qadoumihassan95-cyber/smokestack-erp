"""init control center schema (fleet & platform metadata)

Revision ID: 0001_init
Revises:
Create Date: 2026-07-24

Initial migration for the PFS Control Center's isolated database. Creates the whole
Milestone-1 metadata schema from the model metadata (a standard initial-migration pattern),
giving a real, reversible Alembic history.
"""
from alembic import op

revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    from database import Base
    import models  # noqa: F401  (registers all tables on Base.metadata)
    Base.metadata.create_all(op.get_bind())


def downgrade():
    from database import Base
    import models  # noqa: F401
    Base.metadata.drop_all(op.get_bind())
