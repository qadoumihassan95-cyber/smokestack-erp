"""Scheduled Telegram reports: recipients + delivery log with idempotency

Revision ID: j9e0f1a2b3c4
Revises: i8d9e0f1a2b3
Create Date: 2026-07-19

Two new tables. Nothing existing is altered. The UNIQUE index on
report_deliveries.idem_key is what prevents two Render instances (or a restarted
one) from sending the same scheduled report twice.
"""
from alembic import op
import sqlalchemy as sa

revision = "j9e0f1a2b3c4"
down_revision = "i8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "report_recipients" not in tables:
        op.create_table(
            "report_recipients",
            sa.Column("tg_id", sa.String(), primary_key=True),
            sa.Column("enabled", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("morning", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("evening", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("all_branches", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("per_branch", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("branches", sa.Text()),
            sa.Column("language", sa.String(), server_default="en"),
            sa.Column("include_pdf", sa.Boolean(), server_default=sa.text("false")),
            sa.Column("urgent_alerts", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("updated_by", sa.String()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if "report_deliveries" not in tables:
        op.create_table(
            "report_deliveries",
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                      primary_key=True, autoincrement=True),
            sa.Column("idem_key", sa.String()),
            sa.Column("report_type", sa.String()),
            sa.Column("business_date", sa.Date()),
            sa.Column("scheduled_for", sa.DateTime(timezone=True)),
            sa.Column("sent_at", sa.DateTime(timezone=True)),
            sa.Column("recipient", sa.String()),
            sa.Column("tg_id", sa.String()),
            sa.Column("branch_scope", sa.String()),
            sa.Column("status", sa.String(), server_default="pending"),
            sa.Column("retries", sa.Integer(), server_default="0"),
            sa.Column("error", sa.Text()),
            sa.Column("message_ids", sa.String()),
            sa.Column("pdf_status", sa.String()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("uq_report_idem", "report_deliveries", ["idem_key"], unique=True)
        op.create_index("ix_report_deliveries_tg", "report_deliveries", ["tg_id"])
        op.create_index("ix_report_deliveries_date", "report_deliveries", ["business_date"])


def downgrade():
    op.drop_table("report_deliveries")
    op.drop_table("report_recipients")
