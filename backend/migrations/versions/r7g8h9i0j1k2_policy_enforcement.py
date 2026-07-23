"""Policy enforcement: company_modules.state + feature_flags + policy_overrides.

Revision ID: r7g8h9i0j1k2
Revises: q6f7g8h9i0j1
Create Date: 2026-07-23

ADDITIVE ONLY. One new nullable column (company_modules.state, default 'enabled')
and two new platform tables (feature_flags, policy_overrides). No existing tenant
table is touched; Company #1 keeps every module 'enabled', an active lifetime
subscription and status 'active', so enforcement is a no-op for it. Idempotent
and reversible.
"""
from alembic import op
import sqlalchemy as sa

revision = "r7g8h9i0j1k2"
down_revision = "q6f7g8h9i0j1"
branch_labels = None
depends_on = None

BID = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "company_modules" in tables:
        cols = {c["name"] for c in insp.get_columns("company_modules")}
        if "state" not in cols:
            op.add_column("company_modules",
                          sa.Column("state", sa.String(), nullable=True,
                                    server_default="enabled"))
            op.execute(sa.text("UPDATE company_modules SET state = 'enabled' "
                               "WHERE state IS NULL"))

    if "feature_flags" not in tables:
        op.create_table(
            "feature_flags",
            sa.Column("id", BID, primary_key=True, autoincrement=True),
            sa.Column("key", sa.String()),
            sa.Column("scope", sa.String(), server_default="platform"),
            sa.Column("scope_ref", sa.String()),
            sa.Column("enabled", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("description", sa.Text()),
            sa.Column("updated_by", sa.String()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_feature_flags_key", "feature_flags", ["key"])

    if "policy_overrides" not in tables:
        op.create_table(
            "policy_overrides",
            sa.Column("id", BID, primary_key=True, autoincrement=True),
            sa.Column("company_id", sa.Integer()),
            sa.Column("action", sa.String()),
            sa.Column("allow", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("reason", sa.Text()),
            sa.Column("created_by", sa.String()),
            sa.Column("active", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("expires_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_policy_overrides_company", "policy_overrides", ["company_id"])


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "policy_overrides" in tables:
        op.drop_table("policy_overrides")
    if "feature_flags" in tables:
        op.drop_table("feature_flags")
    if "company_modules" in tables:
        cols = {c["name"] for c in insp.get_columns("company_modules")}
        if "state" in cols:
            op.drop_column("company_modules", "state")
