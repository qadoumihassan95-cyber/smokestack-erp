"""Recurring Telegram reminders: settings + delivery ledger

Revision ID: n3c4d5e6f7g8
Revises: m2b3c4d5e6f7
Create Date: 2026-07-22

Additive. Two new tables: a single-row `reminder_settings` (the whole schedule)
and `reminder_deliveries` (audit + idempotency ledger, UNIQUE idem_key). Nothing
existing is touched, and both are created idempotently so a re-run is safe.
"""
from alembic import op
import sqlalchemy as sa

revision = "n3c4d5e6f7g8"
down_revision = "m2b3c4d5e6f7"
branch_labels = None
depends_on = None

BID = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def upgrade():
    insp = sa.inspect(op.get_bind())
    have = set(insp.get_table_names())

    if "reminder_settings" not in have:
        op.create_table(
            "reminder_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("enabled", sa.Boolean(), server_default=sa.text("false")),
            sa.Column("interval_hours", sa.Integer(), server_default="12"),
            sa.Column("message", sa.Text()),
            sa.Column("active_start_hour", sa.Integer(), server_default="8"),
            sa.Column("active_end_hour", sa.Integer(), server_default="22"),
            sa.Column("paused_days", sa.Text()),
            sa.Column("recipient_mode", sa.String(), server_default="all"),
            sa.Column("recipient_ids", sa.Text()),
            sa.Column("next_run_at", sa.DateTime(timezone=True)),
            sa.Column("last_run_at", sa.DateTime(timezone=True)),
            sa.Column("updated_by", sa.String()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if "reminder_deliveries" not in have:
        op.create_table(
            "reminder_deliveries",
            sa.Column("id", BID, primary_key=True, autoincrement=True),
            sa.Column("idem_key", sa.String()),
            sa.Column("run_at", sa.DateTime(timezone=True)),
            sa.Column("kind", sa.String(), server_default="scheduled"),
            sa.Column("tg_id", sa.String()),
            sa.Column("recipient", sa.String()),
            sa.Column("message", sa.Text()),
            sa.Column("status", sa.String(), server_default="queued"),
            sa.Column("error", sa.Text()),
            sa.Column("message_id", sa.String()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_reminder_deliveries_idem", "reminder_deliveries",
                        ["idem_key"], unique=True)
        op.create_index("ix_reminder_deliveries_tg", "reminder_deliveries", ["tg_id"])
        op.create_index("ix_reminder_deliveries_run", "reminder_deliveries", ["run_at"])


def downgrade():
    op.drop_table("reminder_deliveries")
    op.drop_table("reminder_settings")
