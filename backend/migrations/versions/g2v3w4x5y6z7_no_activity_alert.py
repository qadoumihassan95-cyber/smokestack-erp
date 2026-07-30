"""No-Activity Alert — per-branch schedule/alert config + incident table.

Purely additive & reversible.
 * Adds nullable operating-schedule + alert-config columns to `branches`
   (open_time, close_time, open_days, inactivity_alert_enabled,
   inactivity_threshold_hours). The primary key `branches.name` and every
   existing column/relationship are untouched. NULLs mean "use the documented
   safe default" (08:00–22:00, Mon–Sat, 12 business hours) — no backfill needed.
 * Creates `no_activity_incidents` to store incident lifecycle state so alerts
   and Telegram messages are idempotent.

Telegram idempotency reuses the existing `reminder_deliveries` ledger (a new
`kind='noactivity'` value — no schema change there). Every operation defines a
real downgrade(); dropping the incident table + the added columns fully reverses
the change with no data loss to existing tables.
"""
from alembic import op
import sqlalchemy as sa

revision = "g2v3w4x5y6z7"
down_revision = "f1u2v3w4x5y6"
branch_labels = None
depends_on = None

_BRANCH_COLS = [
    ("open_time", sa.String()),
    ("close_time", sa.String()),
    ("open_days", sa.Text()),
    ("inactivity_alert_enabled", sa.Boolean()),
    ("inactivity_threshold_hours", sa.Integer()),
]


def _insp(bind):
    return sa.inspect(bind)


def _has_col(insp, table, col):
    try:
        return any(c["name"] == col for c in insp.get_columns(table))
    except Exception:
        return False


def upgrade():
    bind = op.get_bind()
    insp = _insp(bind)
    tables = set(insp.get_table_names())

    if "branches" in tables:
        for name, type_ in _BRANCH_COLS:
            if not _has_col(insp, "branches", name):
                op.add_column("branches", sa.Column(name, type_, nullable=True))

    if "no_activity_incidents" not in tables:
        op.create_table(
            "no_activity_incidents",
            sa.Column("company_id", sa.Integer(), nullable=True, server_default="1"),
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                      primary_key=True, autoincrement=True),
            sa.Column("branch", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=True),
            sa.Column("threshold_hours", sa.Integer(), nullable=True),
            sa.Column("business_hours_idle", sa.Numeric(7, 2), nullable=True),
            sa.Column("schedule_source", sa.String(), nullable=True),
            sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_activity_type", sa.String(), nullable=True),
            sa.Column("last_activity_by", sa.String(), nullable=True),
            sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_reminder_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("acknowledged_by", sa.String(), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolved_activity_type", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_no_activity_incidents_company_id", "no_activity_incidents", ["company_id"])
        op.create_index("ix_no_activity_incidents_branch", "no_activity_incidents", ["branch"])
        op.create_index("ix_no_activity_incidents_status", "no_activity_incidents", ["status"])


def downgrade():
    bind = op.get_bind()
    insp = _insp(bind)
    tables = set(insp.get_table_names())

    if "no_activity_incidents" in tables:
        op.drop_table("no_activity_incidents")

    if "branches" in tables:
        with op.batch_alter_table("branches") as batch:
            for name, _type in reversed(_BRANCH_COLS):
                if _has_col(insp, "branches", name):
                    batch.drop_column(name)
