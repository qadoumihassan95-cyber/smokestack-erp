"""mission control foundation: operator ABAC + command log + audit hash-chain + flag version

Revision ID: 0004_mc_foundation
Revises: 0003_dev_platform
Create Date: 2026-07-25

Additive, inspector-guarded migration for Mission Control Milestone 1 (the Foundation triad:
Operator IAM skeleton, Typed Command Pipeline, hash-chained Audit). Safe on a FRESH database
(0001 create_all already made new objects → skip) and on the EXISTING production database
(apply). Fully reversible. No data is transformed; existing rows keep working (legacy operator
falls back to full scope; legacy audit rows are pre-chain genesis).
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_mc_foundation"
down_revision = "0003_dev_platform"
branch_labels = None
depends_on = None


def _has_table(bind, t):
    return sa.inspect(bind).has_table(t)


def _has_col(bind, t, c):
    return _has_table(bind, t) and c in {x["name"] for x in sa.inspect(bind).get_columns(t)}


def _add(bind, table, col):
    if _has_table(bind, table) and not _has_col(bind, table, col.name):
        op.add_column(table, col)


def upgrade():
    bind = op.get_bind()
    _add(bind, "operators", sa.Column("roles", sa.Text, server_default=""))
    _add(bind, "operators", sa.Column("scopes", sa.Text, server_default=""))
    _add(bind, "feature_flags", sa.Column("version", sa.Integer, server_default="1"))
    for c in ("correlation_id", "command_type", "idempotency_key", "prev_hash", "entry_hash"):
        _add(bind, "platform_audit_log", sa.Column(c, sa.String))
    if not _has_table(bind, "command_log"):
        op.create_table(
            "command_log",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("command_type", sa.String, nullable=False, index=True),
            sa.Column("operator_id", sa.String, index=True),
            sa.Column("target", sa.String),
            sa.Column("tenant_context", sa.String),
            sa.Column("environment", sa.String),
            sa.Column("params", sa.Text),
            sa.Column("justification", sa.Text),
            sa.Column("idempotency_key", sa.String, unique=True, index=True),
            sa.Column("expected_version", sa.Integer),
            sa.Column("correlation_id", sa.String, index=True),
            sa.Column("blast_radius", sa.String),
            sa.Column("approval_policy", sa.String),
            sa.Column("approved_by", sa.String),
            sa.Column("status", sa.String, server_default="requested"),
            sa.Column("reason", sa.Text),
            sa.Column("result", sa.Text),
            sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
        )


def downgrade():
    bind = op.get_bind()
    if _has_table(bind, "command_log"):
        op.drop_table("command_log")
    for c in ("entry_hash", "prev_hash", "idempotency_key", "command_type", "correlation_id"):
        if _has_col(bind, "platform_audit_log", c):
            with op.batch_alter_table("platform_audit_log") as b:
                b.drop_column(c)
    if _has_col(bind, "feature_flags", "version"):
        with op.batch_alter_table("feature_flags") as b:
            b.drop_column("version")
    for c in ("scopes", "roles"):
        if _has_col(bind, "operators", c):
            with op.batch_alter_table("operators") as b:
                b.drop_column(c)
