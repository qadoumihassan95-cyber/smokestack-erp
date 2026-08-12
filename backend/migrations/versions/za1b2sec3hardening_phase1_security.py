"""Phase-1 security hardening — session revocation + auth throttling.

Purely additive & reversible.
 * Adds ``users.token_version`` (INT NOT NULL DEFAULT 0). Every issued JWT embeds
   the value current at mint time; advancing it (on password change/reset,
   deactivation, role/branch change, logout) invalidates all prior tokens
   (SS-H-011). Existing rows default to 0, matching legacy tokens, so no session
   is disturbed by the upgrade itself.
 * Creates ``auth_rate_hits`` — one row per throttled attempt, counted within a
   rolling window for a non-reversible scope key, giving a DB-backed,
   multi-instance-safe limiter for ERP/PFS login + Telegram link verification
   (SS-H-007). Rows are pruned on each check so the table stays bounded.

Idempotent: guards against the column/table already existing. downgrade() fully
reverses the change (drops the table + column) with no data loss to existing
tables.
"""
from alembic import op
import sqlalchemy as sa

revision = "za1b2sec3hard"
down_revision = "h3x4y5z6a7b8"
branch_labels = None
depends_on = None


def _insp(bind):
    return sa.inspect(bind)


def _has_col(insp, table, col):
    try:
        return any(c["name"] == col for c in insp.get_columns(table))
    except Exception:
        return False


def _has_table(insp, table):
    try:
        return insp.has_table(table)
    except Exception:
        return table in insp.get_table_names()


def upgrade():
    bind = op.get_bind()
    insp = _insp(bind)

    if not _has_col(insp, "users", "token_version"):
        op.add_column(
            "users",
            sa.Column("token_version", sa.Integer(), nullable=False,
                      server_default="0"),
        )

    if not _has_table(insp, "auth_rate_hits"):
        op.create_table(
            "auth_rate_hits",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("scope_key", sa.String(), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True),
                      server_default=sa.func.now()),
        )
        op.create_index("ix_auth_rate_hits_scope_key", "auth_rate_hits",
                        ["scope_key"])
        op.create_index("ix_auth_rate_hits_ts", "auth_rate_hits", ["ts"])


def downgrade():
    bind = op.get_bind()
    insp = _insp(bind)

    if _has_table(insp, "auth_rate_hits"):
        for ix in ("ix_auth_rate_hits_ts", "ix_auth_rate_hits_scope_key"):
            try:
                op.drop_index(ix, table_name="auth_rate_hits")
            except Exception:
                pass
        op.drop_table("auth_rate_hits")

    if _has_col(insp, "users", "token_version"):
        with op.batch_alter_table("users") as batch:
            batch.drop_column("token_version")
