"""feature flags + feature-flag audit (additive)

Revision ID: 0002_feature_flags
Revises: 0001_init
Create Date: 2026-07-24

Additive migration for the Feature Management milestone. Creates `feature_flags` and
`feature_flag_audit`. Guarded with the inspector so it is safe both on an EXISTING database
(where 0001 already ran and these tables are absent) and on a FRESH database (where 0001's
metadata create_all already created them) — in the latter case the create is skipped.
Fully reversible.
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_feature_flags"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def _has(bind, table):
    return sa.inspect(bind).has_table(table)


def upgrade():
    bind = op.get_bind()
    if not _has(bind, "feature_flags"):
        op.create_table(
            "feature_flags",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("key", sa.String, nullable=False, index=True),
            sa.Column("name", sa.String, nullable=False),
            sa.Column("description", sa.Text),
            sa.Column("erp_product_id", sa.String, sa.ForeignKey("erp_products.id"), nullable=True, index=True),
            sa.Column("module", sa.String),
            sa.Column("visibility", sa.String, nullable=False, server_default="customer"),
            sa.Column("default_state", sa.Boolean, server_default=sa.false()),
            sa.Column("environment_scope", sa.String, server_default="all"),
            sa.Column("customer_allowlist", sa.Text, server_default=""),
            sa.Column("customer_denylist", sa.Text, server_default=""),
            sa.Column("user_allowlist", sa.Text, server_default=""),
            sa.Column("role_requirements", sa.Text, server_default=""),
            sa.Column("license_plan_requirements", sa.Text, server_default=""),
            sa.Column("rollout_percentage", sa.Integer, server_default="0"),
            sa.Column("start_date", sa.DateTime(timezone=True)),
            sa.Column("expiry_date", sa.DateTime(timezone=True)),
            sa.Column("status", sa.String, server_default="active"),
            sa.Column("created_by", sa.String),
            sa.Column("updated_by", sa.String),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
    if not _has(bind, "feature_flag_audit"):
        op.create_table(
            "feature_flag_audit",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("feature_flag_id", sa.Integer, sa.ForeignKey("feature_flags.id"), index=True),
            sa.Column("feature_key", sa.String, index=True),
            sa.Column("erp_product_id", sa.String),
            sa.Column("actor_operator_id", sa.String),
            sa.Column("actor_type", sa.String),
            sa.Column("customer_ref", sa.String),
            sa.Column("environment", sa.String),
            sa.Column("action", sa.String),
            sa.Column("before_state", sa.Text),
            sa.Column("after_state", sa.Text),
            sa.Column("reason", sa.Text),
            sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade():
    bind = op.get_bind()
    if _has(bind, "feature_flag_audit"):
        op.drop_table("feature_flag_audit")
    if _has(bind, "feature_flags"):
        op.drop_table("feature_flags")
