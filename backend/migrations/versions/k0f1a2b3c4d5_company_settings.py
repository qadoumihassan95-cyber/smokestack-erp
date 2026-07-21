"""Company settings (business timezone)

Revision ID: k0f1a2b3c4d5
Revises: j9e0f1a2b3c4
Create Date: 2026-07-20

One new key/value table. The business timezone is seeded from whatever the
branches already have configured, so behaviour is unchanged until an owner
changes it.
"""
from alembic import op
import sqlalchemy as sa

revision = "k0f1a2b3c4d5"
down_revision = "j9e0f1a2b3c4"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "company_settings" not in set(insp.get_table_names()):
        op.create_table(
            "company_settings",
            sa.Column("key", sa.String(), primary_key=True),
            sa.Column("value", sa.Text()),
            sa.Column("updated_by", sa.String()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
    # seed from the existing branch configuration so nothing changes on upgrade
    op.execute("""
        INSERT INTO company_settings (key, value, updated_by)
        SELECT 'business_timezone', COALESCE(MIN(timezone), 'UTC'), 'migration'
        FROM branches
        WHERE NOT EXISTS (SELECT 1 FROM company_settings WHERE key = 'business_timezone')
    """)


def downgrade():
    op.drop_table("company_settings")
