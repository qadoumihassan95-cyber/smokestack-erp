"""Employee Work Schedule: schedules, templates, exceptions, telegram delivery log

Revision ID: o4d5e6f7g8h9
Revises: n3c4d5e6f7g8
Create Date: 2026-07-22

Additive. Four new tables for the weekly scheduling calendar and its
per-employee Telegram delivery. The legacy Employee.sched_* columns (used by
attendance) are deliberately untouched. Idempotent so a re-run is safe.
"""
from alembic import op
import sqlalchemy as sa

revision = "o4d5e6f7g8h9"
down_revision = "n3c4d5e6f7g8"
branch_labels = None
depends_on = None

BID = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def upgrade():
    insp = sa.inspect(op.get_bind())
    have = set(insp.get_table_names())

    if "employee_schedules" not in have:
        op.create_table(
            "employee_schedules",
            sa.Column("id", BID, primary_key=True, autoincrement=True),
            sa.Column("employee_id", sa.String()),
            sa.Column("branch", sa.String()),
            sa.Column("work_date", sa.Date()),
            sa.Column("week_start", sa.Date()),
            sa.Column("start_time", sa.String(), server_default="09:00"),
            sa.Column("end_time", sa.String(), server_default="17:00"),
            sa.Column("break_minutes", sa.Integer(), server_default="0"),
            sa.Column("notes", sa.Text()),
            sa.Column("is_off", sa.Boolean(), server_default=sa.text("false")),
            sa.Column("published", sa.Boolean(), server_default=sa.text("false")),
            sa.Column("template_id", BID),
            sa.Column("created_by", sa.String()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_emp_sched_emp", "employee_schedules", ["employee_id"])
        op.create_index("ix_emp_sched_date", "employee_schedules", ["work_date"])
        op.create_index("ix_emp_sched_week", "employee_schedules", ["week_start"])
        op.create_index("ix_emp_sched_branch", "employee_schedules", ["branch"])

    if "schedule_templates" not in have:
        op.create_table(
            "schedule_templates",
            sa.Column("id", BID, primary_key=True, autoincrement=True),
            sa.Column("name", sa.String()),
            sa.Column("recurrence", sa.String(), server_default="weekly"),
            sa.Column("weekdays", sa.Text()),
            sa.Column("every_other", sa.Boolean(), server_default=sa.text("false")),
            sa.Column("start_time", sa.String(), server_default="09:00"),
            sa.Column("end_time", sa.String(), server_default="17:00"),
            sa.Column("break_minutes", sa.Integer(), server_default="0"),
            sa.Column("notes", sa.Text()),
            sa.Column("created_by", sa.String()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if "schedule_exceptions" not in have:
        op.create_table(
            "schedule_exceptions",
            sa.Column("id", BID, primary_key=True, autoincrement=True),
            sa.Column("employee_id", sa.String()),
            sa.Column("work_date", sa.Date()),
            sa.Column("kind", sa.String(), server_default="off"),
            sa.Column("start_time", sa.String()),
            sa.Column("end_time", sa.String()),
            sa.Column("break_minutes", sa.Integer(), server_default="0"),
            sa.Column("reason", sa.Text()),
            sa.Column("created_by", sa.String()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_sched_exc_emp", "schedule_exceptions", ["employee_id"])
        op.create_index("ix_sched_exc_date", "schedule_exceptions", ["work_date"])

    if "telegram_delivery_log" not in have:
        op.create_table(
            "telegram_delivery_log",
            sa.Column("id", BID, primary_key=True, autoincrement=True),
            sa.Column("idem_key", sa.String()),
            sa.Column("employee_id", sa.String()),
            sa.Column("tg_id", sa.String()),
            sa.Column("recipient", sa.String()),
            sa.Column("week_start", sa.Date()),
            sa.Column("kind", sa.String(), server_default="publish"),
            sa.Column("status", sa.String(), server_default="queued"),
            sa.Column("message", sa.Text()),
            sa.Column("error", sa.Text()),
            sa.Column("message_id", sa.String()),
            sa.Column("created_by", sa.String()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_tg_dlog_idem", "telegram_delivery_log", ["idem_key"], unique=True)
        op.create_index("ix_tg_dlog_emp", "telegram_delivery_log", ["employee_id"])
        op.create_index("ix_tg_dlog_tg", "telegram_delivery_log", ["tg_id"])
        op.create_index("ix_tg_dlog_week", "telegram_delivery_log", ["week_start"])


def downgrade():
    op.drop_table("telegram_delivery_log")
    op.drop_table("schedule_exceptions")
    op.drop_table("schedule_templates")
    op.drop_table("employee_schedules")
