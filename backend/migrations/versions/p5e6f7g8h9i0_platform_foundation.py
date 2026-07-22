"""PFS Platform foundation: platform_users, applications, companies, modules,
company_modules, subscriptions, platform_audit

Revision ID: p5e6f7g8h9i0
Revises: o4d5e6f7g8h9
Create Date: 2026-07-22

Phase 0 of the multi-tenant migration. ADDITIVE ONLY — seven new platform tables.
No existing tenant table is touched, so existing functionality is unaffected and
rollback is a redeploy. Idempotent (safe to re-run).
"""
from alembic import op
import sqlalchemy as sa

revision = "p5e6f7g8h9i0"
down_revision = "o4d5e6f7g8h9"
branch_labels = None
depends_on = None

BID = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def upgrade():
    insp = sa.inspect(op.get_bind())
    have = set(insp.get_table_names())

    if "platform_users" not in have:
        op.create_table(
            "platform_users",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("username", sa.String()),
            sa.Column("name", sa.String()),
            sa.Column("password_hash", sa.String()),
            sa.Column("active", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("must_change_password", sa.Boolean(), server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("last_login", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_platform_users_username", "platform_users", ["username"], unique=True)

    if "applications" not in have:
        op.create_table(
            "applications",
            sa.Column("key", sa.String(), primary_key=True),
            sa.Column("name", sa.String()),
            sa.Column("industry", sa.String()),
            sa.Column("description", sa.Text()),
            sa.Column("active", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if "companies" not in have:
        op.create_table(
            "companies",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String()),
            sa.Column("slug", sa.String()),
            sa.Column("industry", sa.String()),
            sa.Column("application_key", sa.String(), server_default="smoke_shop"),
            sa.Column("owner_user_id", sa.String()),
            sa.Column("status", sa.String(), server_default="active"),
            sa.Column("version", sa.String()),
            sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("suspended_at", sa.DateTime(timezone=True)),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
            sa.Column("deleted_at", sa.DateTime(timezone=True)),
            sa.Column("last_activity", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_companies_slug", "companies", ["slug"], unique=True)

    if "modules" not in have:
        op.create_table(
            "modules",
            sa.Column("key", sa.String(), primary_key=True),
            sa.Column("name", sa.String()),
            sa.Column("category", sa.String()),
            sa.Column("application_key", sa.String(), server_default="core"),
            sa.Column("depends_on", sa.Text()),
            sa.Column("default_enabled", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("is_beta", sa.Boolean(), server_default=sa.text("false")),
            sa.Column("version", sa.String()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if "company_modules" not in have:
        op.create_table(
            "company_modules",
            sa.Column("id", BID, primary_key=True, autoincrement=True),
            sa.Column("company_id", sa.Integer()),
            sa.Column("module_key", sa.String()),
            sa.Column("enabled", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("source", sa.String(), server_default="global"),
            sa.Column("updated_by", sa.String()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_company_modules_company", "company_modules", ["company_id"])
        op.create_index("ix_company_modules_module", "company_modules", ["module_key"])

    if "subscriptions" not in have:
        op.create_table(
            "subscriptions",
            sa.Column("id", BID, primary_key=True, autoincrement=True),
            sa.Column("company_id", sa.Integer()),
            sa.Column("plan", sa.String(), server_default="trial"),
            sa.Column("status", sa.String(), server_default="active"),
            sa.Column("trial_ends", sa.Date()),
            sa.Column("period_start", sa.Date()),
            sa.Column("period_end", sa.Date()),
            sa.Column("gateway", sa.String()),
            sa.Column("gateway_customer_id", sa.String()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_subscriptions_company", "subscriptions", ["company_id"])

    if "platform_audit" not in have:
        op.create_table(
            "platform_audit",
            sa.Column("id", BID, primary_key=True, autoincrement=True),
            sa.Column("super_admin_id", sa.String()),
            sa.Column("action", sa.String()),
            sa.Column("entity", sa.String()),
            sa.Column("ref", sa.String()),
            sa.Column("company_id", sa.Integer()),
            sa.Column("detail", sa.Text()),
            sa.Column("prev_value", sa.Text()),
            sa.Column("new_value", sa.Text()),
            sa.Column("ip", sa.String()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_platform_audit_admin", "platform_audit", ["super_admin_id"])
        op.create_index("ix_platform_audit_company", "platform_audit", ["company_id"])


def downgrade():
    op.drop_table("platform_audit")
    op.drop_table("subscriptions")
    op.drop_table("company_modules")
    op.drop_table("modules")
    op.drop_table("companies")
    op.drop_table("applications")
    op.drop_table("platform_users")
