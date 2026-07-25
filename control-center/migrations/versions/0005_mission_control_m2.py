"""mission control M2: approvals, break-glass, sessions, outbox, CQRS read model

Revision ID: 0005_mc_m2
Revises: 0004_mc_foundation
Create Date: 2026-07-25

Additive, inspector-guarded, reversible. Deepens the platform foundation (governance depth,
event backbone, first CQRS read model, operator sessions). No existing column is modified except
the additive `operators.mfa_enabled`. Safe on fresh (create_all already made objects → skip) and
existing DBs.
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_mc_m2"
down_revision = "0004_mc_foundation"
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
    if _has_table(bind, "operators") and not _has_col(bind, "operators", "mfa_enabled"):
        op.add_column("operators", sa.Column("mfa_enabled", sa.Boolean, server_default=sa.false()))

    if not _has_table(bind, "approval_requests"):
        op.create_table(
            "approval_requests",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("subject_type", sa.String, nullable=False),
            sa.Column("subject_ref", sa.String),
            sa.Column("requested_by", sa.String, index=True),
            sa.Column("policy", sa.String, server_default="single"),
            sa.Column("quorum_required", sa.Integer, server_default="1"),
            sa.Column("status", sa.String, server_default="pending"),
            sa.Column("reason", sa.Text),
            sa.Column("correlation_id", sa.String, index=True),
            sa.Column("expires_at", _dt()),
            sa.Column("created_at", _dt(), server_default=sa.func.now()),
            sa.Column("decided_at", _dt()),
        )
    if not _has_table(bind, "approvals"):
        op.create_table(
            "approvals",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("request_id", sa.Integer, sa.ForeignKey("approval_requests.id"), index=True),
            sa.Column("approver_id", sa.String, index=True),
            sa.Column("decision", sa.String),
            sa.Column("reason", sa.Text),
            sa.Column("sequence", sa.Integer, server_default="0"),
            sa.Column("at", _dt(), server_default=sa.func.now()),
        )
    if not _has_table(bind, "elevation_grants"):
        op.create_table(
            "elevation_grants",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("operator_id", sa.String, index=True),
            sa.Column("capability", sa.String),
            sa.Column("reason", sa.Text),
            sa.Column("status", sa.String, server_default="pending"),
            sa.Column("approval_request_id", sa.Integer, sa.ForeignKey("approval_requests.id"), nullable=True),
            sa.Column("offline", sa.Boolean, server_default=sa.false()),
            sa.Column("recording_ref", sa.String),
            sa.Column("correlation_id", sa.String, index=True),
            sa.Column("created_at", _dt(), server_default=sa.func.now()),
            sa.Column("activated_at", _dt()),
            sa.Column("expires_at", _dt()),
            sa.Column("revoked_at", _dt()),
            sa.Column("revoked_reason", sa.String),
        )
    if not _has_table(bind, "operator_sessions"):
        op.create_table(
            "operator_sessions",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("operator_id", sa.String, index=True),
            sa.Column("jti", sa.String, unique=True, index=True),
            sa.Column("device", sa.String),
            sa.Column("ip", sa.String),
            sa.Column("mfa_state", sa.String, server_default="none"),
            sa.Column("break_glass", sa.Boolean, server_default=sa.false()),
            sa.Column("status", sa.String, server_default="active"),
            sa.Column("recording_ref", sa.String),
            sa.Column("created_at", _dt(), server_default=sa.func.now()),
            sa.Column("last_seen_at", _dt(), server_default=sa.func.now()),
            sa.Column("expires_at", _dt()),
            sa.Column("revoked_at", _dt()),
        )
    if not _has_table(bind, "outbox"):
        op.create_table(
            "outbox",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("aggregate_type", sa.String, index=True),
            sa.Column("aggregate_id", sa.String),
            sa.Column("event_type", sa.String, nullable=False, index=True),
            sa.Column("event_version", sa.Integer, server_default="1"),
            sa.Column("payload", sa.Text),
            sa.Column("correlation_id", sa.String, index=True),
            sa.Column("causation_id", sa.String),
            sa.Column("dedupe_key", sa.String, unique=True),
            sa.Column("status", sa.String, server_default="pending"),
            sa.Column("attempts", sa.Integer, server_default="0"),
            sa.Column("max_attempts", sa.Integer, server_default="5"),
            sa.Column("available_at", _dt(), server_default=sa.func.now()),
            sa.Column("created_at", _dt(), server_default=sa.func.now()),
            sa.Column("published_at", _dt()),
            sa.Column("last_error", sa.Text),
        )
    if not _has_table(bind, "read_model_state"):
        op.create_table(
            "read_model_state",
            sa.Column("name", sa.String, primary_key=True),
            sa.Column("last_event_id", sa.Integer, server_default="0"),
            sa.Column("status", sa.String, server_default="live"),
            sa.Column("updated_at", _dt(), server_default=sa.func.now()),
            sa.Column("rebuilt_at", _dt()),
        )
    if not _has_table(bind, "rm_command_feed"):
        op.create_table(
            "rm_command_feed",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("command_type", sa.String, index=True),
            sa.Column("operator_id", sa.String, index=True),
            sa.Column("target", sa.String),
            sa.Column("status", sa.String),
            sa.Column("blast_radius", sa.String),
            sa.Column("correlation_id", sa.String),
            sa.Column("occurred_at", _dt()),
        )


def downgrade():
    bind = op.get_bind()
    for t in ("rm_command_feed", "read_model_state", "outbox", "operator_sessions",
              "elevation_grants", "approvals", "approval_requests"):
        if _has_table(bind, t):
            op.drop_table(t)
    if _has_col(bind, "operators", "mfa_enabled"):
        with op.batch_alter_table("operators") as b:
            b.drop_column("mfa_enabled")
