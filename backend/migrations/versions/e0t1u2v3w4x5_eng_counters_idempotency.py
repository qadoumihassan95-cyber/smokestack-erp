"""Engineering platform tables — document counters + idempotency store.

Revision ID: e0t1u2v3w4x5
Revises: d9s0t1u2v3w4
Create Date: 2026-07-24

Purely additive: two new platform tables. No existing table, FK-root, or business
data is touched. Idempotent + reversible.
  * document_counters  — per-company monotonic document numbers (Phase 4)
  * idempotency_keys   — shared idempotency store (Phase 5)
"""
from alembic import op
import sqlalchemy as sa

revision = "e0t1u2v3w4x5"
down_revision = "d9s0t1u2v3w4"
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    have = set(insp.get_table_names())
    big = sa.BigInteger().with_variant(sa.Integer, "sqlite")
    if "document_counters" not in have:
        op.create_table(
            "document_counters",
            sa.Column("company_id", sa.Integer(), nullable=False),
            sa.Column("doc_type", sa.String(), nullable=False),
            sa.Column("next_val", big, server_default="0"),
            sa.PrimaryKeyConstraint("company_id", "doc_type"),
        )
    if "idempotency_keys" not in have:
        op.create_table(
            "idempotency_keys",
            sa.Column("scope", sa.String(), nullable=False),
            sa.Column("key", sa.String(), nullable=False),
            sa.Column("method", sa.String()),
            sa.Column("path", sa.String()),
            sa.Column("status_code", sa.Integer()),
            sa.Column("response_body", sa.Text()),
            sa.Column("content_type", sa.String()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("scope", "key"),
        )


def downgrade():
    insp = sa.inspect(op.get_bind())
    have = set(insp.get_table_names())
    if "idempotency_keys" in have:
        op.drop_table("idempotency_keys")
    if "document_counters" in have:
        op.drop_table("document_counters")
