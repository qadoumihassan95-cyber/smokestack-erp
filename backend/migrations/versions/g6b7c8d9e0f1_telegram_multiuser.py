"""Telegram Management Center: multi-user account fields + richer audit context

Revision ID: g6b7c8d9e0f1
Revises: f5a6b7c8d9e0
Create Date: 2026-07-18

Additive only. Existing telegram_links rows are backfilled to status='active'
so every account that works today keeps working.
"""
from alembic import op
import sqlalchemy as sa

revision = "g6b7c8d9e0f1"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)

    tl = {c["name"] for c in insp.get_columns("telegram_links")}
    add_tl = [(n, t) for n, t in [
        ("status", sa.String()), ("employee_id", sa.String()), ("linked_by", sa.String()),
        ("disabled_at", sa.DateTime(timezone=True)), ("disabled_by", sa.String()),
    ] if n not in tl]
    if add_tl:
        with op.batch_alter_table("telegram_links") as b:
            for n, t in add_tl:
                b.add_column(sa.Column(n, t, nullable=True))
    # backward compatibility: every pre-existing link stays active
    op.execute("UPDATE telegram_links SET status='active' WHERE status IS NULL")

    al = {c["name"] for c in insp.get_columns("audit_log")}
    add_al = [(n, sa.String()) for n in ("tg_username", "branch", "role", "ip") if n not in al]
    if add_al:
        with op.batch_alter_table("audit_log") as b:
            for n, t in add_al:
                b.add_column(sa.Column(n, t, nullable=True))


def downgrade():
    with op.batch_alter_table("audit_log") as b:
        for c in ("ip", "role", "branch", "tg_username"):
            b.drop_column(c)
    with op.batch_alter_table("telegram_links") as b:
        for c in ("disabled_by", "disabled_at", "linked_by", "employee_id", "status"):
            b.drop_column(c)
