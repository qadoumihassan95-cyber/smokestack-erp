"""developer platform: feature lifecycle_stage + dev_preview_sessions (additive)

Revision ID: 0003_dev_platform
Revises: 0002_feature_flags
Create Date: 2026-07-24

Additive migration for the Internal Development Platform milestone. Adds
`feature_flags.lifecycle_stage` and creates `dev_preview_sessions`. Inspector-guarded so it
is safe on a FRESH database (0001 create_all already made the objects → skip) and on an
EXISTING production database (apply them). Fully reversible.
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_dev_platform"
down_revision = "0002_feature_flags"
branch_labels = None
depends_on = None


def _has_table(bind, table):
    return sa.inspect(bind).has_table(table)


def _has_col(bind, table, col):
    if not _has_table(bind, table):
        return False
    return col in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade():
    bind = op.get_bind()
    if _has_table(bind, "feature_flags") and not _has_col(bind, "feature_flags", "lifecycle_stage"):
        op.add_column("feature_flags",
                      sa.Column("lifecycle_stage", sa.String, server_default="development"))
    if not _has_table(bind, "dev_preview_sessions"):
        op.create_table(
            "dev_preview_sessions",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("operator_id", sa.String, index=True),
            sa.Column("erp_product_id", sa.String, sa.ForeignKey("erp_products.id"), index=True),
            sa.Column("customer_ref", sa.String),
            sa.Column("customer_name", sa.String),
            sa.Column("environment", sa.String, server_default="development"),
            sa.Column("feature_profile", sa.String, server_default="platform_owner"),
            sa.Column("status", sa.String, server_default="active"),
            sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("ended_at", sa.DateTime(timezone=True)),
        )


def downgrade():
    bind = op.get_bind()
    if _has_table(bind, "dev_preview_sessions"):
        op.drop_table("dev_preview_sessions")
    if _has_col(bind, "feature_flags", "lifecycle_stage"):
        with op.batch_alter_table("feature_flags") as b:
            b.drop_column("lifecycle_stage")
