"""Candidate 1: durable transfer identity (BF-13) + payroll period uniqueness (SIM-06).

BF-13  movements.transfer_id — both legs of a transfer carry the transfer's id so a
       pair can be verified on source, destination, SKU and quantity instead of a
       global count of rows. Rows written before this column existed keep NULL and
       are reported as unverifiable by the validator; they are NEVER back-filled by
       guesswork, because inventing an identity would manufacture the very evidence
       the check exists to find.

SIM-06 payroll_runs — the natural key of a finalized pay period, with
       UNIQUE(company_id, branch, period_start, period_end). The database refuses a
       duplicate finalize; an application pre-check cannot, because two concurrent
       transactions both read "not finalized" and both insert.

Upgrade safety: additive only. The new column is nullable, the new table is empty,
and no existing row is rewritten. Downgrade drops both.
"""
from alembic import op
import sqlalchemy as sa

revision = "zb2c3cand1"
down_revision = "za1b2sec3hard"
branch_labels = None
depends_on = None


def _has_table(bind, name):
    return sa.inspect(bind).has_table(name)


def _has_column(bind, table, column):
    if not _has_table(bind, table):
        return False
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade():
    bind = op.get_bind()

    if not _has_column(bind, "movements", "transfer_id"):
        op.add_column("movements", sa.Column("transfer_id", sa.String(), nullable=True))
        op.create_index("ix_movements_transfer_id", "movements", ["transfer_id"])

    if not _has_table(bind, "payroll_runs"):
        op.create_table(
            "payroll_runs",
            sa.Column("row_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                      primary_key=True, autoincrement=True),
            sa.Column("company_id", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("branch", sa.String(), nullable=False),
            sa.Column("period_start", sa.Date(), nullable=False),
            sa.Column("period_end", sa.Date(), nullable=False),
            sa.Column("gross", sa.Numeric(12, 2), nullable=False),
            sa.Column("ledger_id", sa.BigInteger().with_variant(sa.Integer, "sqlite")),
            sa.Column("finalized_by", sa.String()),
            sa.Column("finalized_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("company_id", "branch", "period_start", "period_end",
                                name="uq_payroll_runs_period"),
        )
        op.create_index("ix_payroll_runs_company_id", "payroll_runs", ["company_id"])
        op.create_index("ix_payroll_runs_branch", "payroll_runs", ["branch"])


def downgrade():
    bind = op.get_bind()

    if _has_table(bind, "payroll_runs"):
        op.drop_index("ix_payroll_runs_branch", table_name="payroll_runs")
        op.drop_index("ix_payroll_runs_company_id", table_name="payroll_runs")
        op.drop_table("payroll_runs")

    if _has_column(bind, "movements", "transfer_id"):
        op.drop_index("ix_movements_transfer_id", table_name="movements")
        op.drop_column("movements", "transfer_id")
