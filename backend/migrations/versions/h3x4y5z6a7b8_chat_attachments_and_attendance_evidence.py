"""Team Chat image attachments + Telegram attendance evidence (location + selfie).

Revision ID: h3x4y5z6a7b8
Revises: g2v3w4x5y6z7
Create Date: 2026-08-04

Purely additive & reversible. Adds two independent tables and touches nothing
existing:

  * chat_attachments      — durable Postgres image storage for Team Chat
                            (clean re-encoded bytes + thumbnail; never a public
                            URL, never base64 in the message row).
  * attendance_evidence   — one short-lived Telegram clock-in attempt binding a
                            location + a freshly captured selfie to an attendance
                            record; selfie bytes stored in Postgres.

No key, FK, ledger row, permission, or historical record is modified. downgrade
drops only the two new tables.
"""
from alembic import op
import sqlalchemy as sa

revision = "h3x4y5z6a7b8"
down_revision = "g2v3w4x5y6z7"
branch_labels = None
depends_on = None

# BigInteger identity on Postgres, plain Integer on SQLite (mirrors the models).
_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _tables(insp):
    try:
        return set(insp.get_table_names())
    except Exception:
        return set()


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    have = _tables(insp)

    if "chat_attachments" not in have:
        op.create_table(
            "chat_attachments",
            sa.Column("company_id", sa.Integer(), index=True, nullable=True, server_default="1"),
            sa.Column("id", _BIGINT, primary_key=True, autoincrement=True),
            sa.Column("message_id", sa.BigInteger(), index=True),
            sa.Column("room_id", sa.BigInteger(), index=True),
            sa.Column("uploader_id", sa.String(), index=True),
            sa.Column("kind", sa.String(), server_default="image"),
            sa.Column("mime", sa.String()),
            sa.Column("filename", sa.String()),
            sa.Column("size_bytes", sa.Integer()),
            sa.Column("width", sa.Integer()),
            sa.Column("height", sa.Integer()),
            sa.Column("sha256", sa.String(), index=True),
            sa.Column("data", sa.LargeBinary()),
            sa.Column("thumb", sa.LargeBinary()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("deleted", sa.Boolean(), server_default=sa.false()),
        )

    if "attendance_evidence" not in have:
        op.create_table(
            "attendance_evidence",
            sa.Column("company_id", sa.Integer(), index=True, nullable=True, server_default="1"),
            sa.Column("id", _BIGINT, primary_key=True, autoincrement=True),
            sa.Column("attempt_id", sa.String(), unique=True, index=True),
            sa.Column("employee_id", sa.String(), index=True),
            sa.Column("employee_name", sa.String()),
            sa.Column("tg_id", sa.String(), index=True),
            sa.Column("branch", sa.String(), index=True),
            sa.Column("attendance_id", sa.BigInteger(), index=True),
            sa.Column("status", sa.String(), server_default="pending_location"),
            sa.Column("lat", sa.Numeric(10, 6)),
            sa.Column("lng", sa.Numeric(10, 6)),
            sa.Column("loc_msg_id", sa.String()),
            sa.Column("loc_at", sa.DateTime(timezone=True)),
            sa.Column("dist_m", sa.Integer()),
            sa.Column("out_of_area", sa.Boolean(), server_default=sa.false()),
            sa.Column("selfie_msg_id", sa.String()),
            sa.Column("selfie_file_id", sa.String()),
            sa.Column("selfie_mime", sa.String()),
            sa.Column("selfie", sa.LargeBinary()),
            sa.Column("selfie_sha256", sa.String()),
            sa.Column("selfie_at", sa.DateTime(timezone=True)),
            sa.Column("consumed", sa.Boolean(), server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("expires_at", sa.DateTime(timezone=True)),
            sa.Column("retain_until", sa.DateTime(timezone=True)),
        )


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    have = _tables(insp)
    if "attendance_evidence" in have:
        op.drop_table("attendance_evidence")
    if "chat_attachments" in have:
        op.drop_table("chat_attachments")
