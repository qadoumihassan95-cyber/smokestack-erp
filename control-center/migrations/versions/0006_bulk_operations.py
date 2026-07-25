"""mission control M3: bulk-operations safety engine (segments, change_jobs, change_targets)

Revision ID: 0006_bulk_ops
Revises: 0005_mc_m2
Create Date: 2026-07-25

Additive, inspector-guarded, reversible. Adds the fleet-orchestration tables and two additive
columns on customer_refs (region, version — optimistic-concurrency token for per-target bulk ops).
Safe on fresh + existing DBs; live SmokeStack / Company #1 unaffected.
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_bulk_ops"
down_revision = "0005_mc_m2"
branch_labels = None
depends_on = None


def _has_table(bind, t):
    return sa.inspect(bind).has_table(t)


def _has_col(bind, t, c):
    return _has_table(bind, t) and c in {x["name"] for x in sa.inspect(bind).get_columns(t)}


def _dt():
    return sa.DateTime(timezone=True)


def upgrade():
    bind = op.get_bind()
    if _has_table(bind, "customer_refs") and not _has_col(bind, "customer_refs", "region"):
        op.add_column("customer_refs", sa.Column("region", sa.String, server_default="us"))
    if _has_table(bind, "customer_refs") and not _has_col(bind, "customer_refs", "version"):
        op.add_column("customer_refs", sa.Column("version", sa.Integer, server_default="1"))

    if not _has_table(bind, "segments"):
        op.create_table(
            "segments",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("name", sa.String, nullable=False),
            sa.Column("description", sa.Text),
            sa.Column("filters", sa.Text, server_default="{}"),
            sa.Column("saved", sa.Boolean, server_default=sa.true()),
            sa.Column("created_by", sa.String),
            sa.Column("created_at", _dt(), server_default=sa.func.now()),
        )
    if not _has_table(bind, "change_jobs"):
        op.create_table(
            "change_jobs",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("name", sa.String),
            sa.Column("command_type", sa.String, nullable=False),
            sa.Column("params", sa.Text, server_default="{}"),
            sa.Column("filters", sa.Text, server_default="{}"),
            sa.Column("reason", sa.Text),
            sa.Column("blast_radius", sa.String),
            sa.Column("data_class", sa.String),
            sa.Column("approval_policy", sa.String, server_default="none"),
            sa.Column("approval_request_id", sa.Integer, sa.ForeignKey("approval_requests.id"), nullable=True),
            sa.Column("rollback_required", sa.Boolean, server_default=sa.false()),
            sa.Column("rings", sa.Text, server_default="[]"),
            sa.Column("current_ring", sa.Integer, server_default="0"),
            sa.Column("rate_limit_per_tick", sa.Integer, server_default="50"),
            sa.Column("maintenance_window", sa.Text),
            sa.Column("error_budget", sa.String, server_default="0.2"),
            sa.Column("status", sa.String, server_default="planned"),
            sa.Column("halt_reason", sa.String),
            sa.Column("total_targets", sa.Integer, server_default="0"),
            sa.Column("created_by", sa.String, index=True),
            sa.Column("correlation_id", sa.String, index=True),
            sa.Column("created_at", _dt(), server_default=sa.func.now()),
            sa.Column("started_at", _dt()),
            sa.Column("completed_at", _dt()),
        )
    if not _has_table(bind, "change_targets"):
        op.create_table(
            "change_targets",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("job_id", sa.Integer, sa.ForeignKey("change_jobs.id"), index=True),
            sa.Column("target_type", sa.String, server_default="customer"),
            sa.Column("target_ref", sa.String, index=True),
            sa.Column("expected_version", sa.Integer),
            sa.Column("planned_state", sa.Text),
            sa.Column("ring", sa.Integer, server_default="0"),
            sa.Column("status", sa.String, server_default="pending"),
            sa.Column("attempts", sa.Integer, server_default="0"),
            sa.Column("idempotency_key", sa.String, unique=True),
            sa.Column("result", sa.Text),
            sa.Column("error", sa.String),
            sa.Column("at", _dt(), server_default=sa.func.now()),
        )


def downgrade():
    bind = op.get_bind()
    for t in ("change_targets", "change_jobs", "segments"):
        if _has_table(bind, t):
            op.drop_table(t)
    for c in ("version", "region"):
        if _has_col(bind, "customer_refs", c):
            with op.batch_alter_table("customer_refs") as b:
                b.drop_column(c)
