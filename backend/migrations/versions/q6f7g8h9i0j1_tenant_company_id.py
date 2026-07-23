"""Tenantize: add nullable company_id to every tenant-owned table and backfill
existing rows to Company #1.

Revision ID: q6f7g8h9i0j1
Revises: p5e6f7g8h9i0
Create Date: 2026-07-23

Phase 1 of the multi-tenant migration. ADDITIVE ONLY and fully backward
compatible:
  * company_id is added as NULLABLE with a server default of 1, so existing
    INSERTs that do not name it keep working and land in Company #1;
  * every existing row is backfilled to Company #1;
  * an index is created on each company_id for scoped queries.

Intentionally DEFERRED to a later migration (after production verification):
  * NOT NULL on company_id,
  * FOREIGN KEY company_id -> companies.id
    (Company #1's row is seeded at application startup, i.e. AFTER preDeploy
    migrations, so a validated FK here could run before the row exists).

Idempotent: re-running skips tables/columns/indexes that already exist. Rollback
is a redeploy of the previous revision (the columns are unused by that code).
"""
from alembic import op
import sqlalchemy as sa

revision = "q6f7g8h9i0j1"
down_revision = "p5e6f7g8h9i0"
branch_labels = None
depends_on = None

# Every tenant-owned table. Platform tables (companies, applications, modules,
# company_modules, subscriptions, platform_users, platform_audit) are NOT here.
TENANT_TABLES = [
    "branches", "attendance", "users", "user_branches", "products", "stock",
    "movements", "ledger", "employees", "licenses", "purchases", "transfers",
    "customers", "suppliers", "approvals", "clock_events", "audit_log",
    "telegram_links", "link_codes", "validation_runs", "report_recipients",
    "report_deliveries", "company_settings", "chat_rooms", "chat_members",
    "chat_messages", "chat_reactions", "chat_presence", "chat_tasks",
    "chat_announcements", "reminder_settings", "reminder_deliveries",
    "employee_schedules", "schedule_templates", "schedule_exceptions",
    "telegram_delivery_log",
]


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_tables = set(insp.get_table_names())

    for table in TENANT_TABLES:
        if table not in existing_tables:
            continue  # table not present yet (fresh env builds it from models)
        cols = {c["name"] for c in insp.get_columns(table)}
        if "company_id" not in cols:
            op.add_column(table, sa.Column("company_id", sa.Integer(),
                                           nullable=True, server_default="1"))
        # backfill any NULLs to Company #1 (idempotent)
        op.execute(sa.text(
            f'UPDATE {table} SET company_id = 1 WHERE company_id IS NULL'))
        # index for scoped queries (idempotent)
        idx = f"ix_{table}_company_id"
        existing_idx = {i["name"] for i in insp.get_indexes(table)}
        if idx not in existing_idx:
            op.create_index(idx, table, ["company_id"])


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_tables = set(insp.get_table_names())
    for table in TENANT_TABLES:
        if table not in existing_tables:
            continue
        existing_idx = {i["name"] for i in insp.get_indexes(table)}
        idx = f"ix_{table}_company_id"
        if idx in existing_idx:
            op.drop_index(idx, table_name=table)
        cols = {c["name"] for c in insp.get_columns(table)}
        if "company_id" in cols:
            op.drop_column(table, "company_id")
