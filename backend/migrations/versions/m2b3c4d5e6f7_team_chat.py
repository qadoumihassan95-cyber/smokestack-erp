"""Team Chat: rooms, members, messages, reactions, presence, tasks, announcements

Revision ID: m2b3c4d5e6f7
Revises: l1a2b3c4d5e6
Create Date: 2026-07-21

Additive. Seven new tables plus indexes tuned for the two hot paths: pulling a
room's recent messages, and pulling messages newer than a cursor (the poll).
Nothing existing is touched.
"""
from alembic import op
import sqlalchemy as sa

revision = "m2b3c4d5e6f7"
down_revision = "l1a2b3c4d5e6"
branch_labels = None
depends_on = None

BID = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def upgrade():
    insp = sa.inspect(op.get_bind())
    have = set(insp.get_table_names())

    if "chat_rooms" not in have:
        op.create_table("chat_rooms",
            sa.Column("id", BID, primary_key=True, autoincrement=True),
            sa.Column("kind", sa.String(), server_default="group"),
            sa.Column("name", sa.String()),
            sa.Column("branch", sa.String()),
            sa.Column("department", sa.String()),
            sa.Column("created_by", sa.String()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("archived", sa.Boolean(), server_default=sa.text("false")))
        op.create_index("ix_chat_rooms_branch", "chat_rooms", ["branch"])

    if "chat_members" not in have:
        op.create_table("chat_members",
            sa.Column("id", BID, primary_key=True, autoincrement=True),
            sa.Column("room_id", BID),
            sa.Column("user_id", sa.String()),
            sa.Column("role", sa.String(), server_default="member"),
            sa.Column("last_read_id", BID, server_default="0"),
            sa.Column("muted", sa.Boolean(), server_default=sa.text("false")),
            sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
        op.create_index("ix_chat_members_room", "chat_members", ["room_id"])
        op.create_index("ix_chat_members_user", "chat_members", ["user_id"])

    if "chat_messages" not in have:
        op.create_table("chat_messages",
            sa.Column("id", BID, primary_key=True, autoincrement=True),
            sa.Column("room_id", BID),
            sa.Column("user_id", sa.String()),
            sa.Column("body", sa.Text()),
            sa.Column("kind", sa.String(), server_default="text"),
            sa.Column("erp_ref", sa.String()),
            sa.Column("reply_to", BID),
            sa.Column("pinned", sa.Boolean(), server_default=sa.text("false")),
            sa.Column("edited", sa.Boolean(), server_default=sa.text("false")),
            sa.Column("deleted", sa.Boolean(), server_default=sa.text("false")),
            sa.Column("mentions", sa.String()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("edited_at", sa.DateTime(timezone=True)))
        # the two hot paths: recent messages, and messages past a cursor
        op.create_index("ix_chat_messages_room_id", "chat_messages", ["room_id", "id"])

    if "chat_reactions" not in have:
        op.create_table("chat_reactions",
            sa.Column("id", BID, primary_key=True, autoincrement=True),
            sa.Column("message_id", BID),
            sa.Column("user_id", sa.String()),
            sa.Column("emoji", sa.String()))
        op.create_index("ix_chat_reactions_msg", "chat_reactions", ["message_id"])

    if "chat_presence" not in have:
        op.create_table("chat_presence",
            sa.Column("user_id", sa.String(), primary_key=True),
            sa.Column("last_seen", sa.DateTime(timezone=True)),
            sa.Column("typing_room", BID),
            sa.Column("typing_at", sa.DateTime(timezone=True)))

    if "chat_tasks" not in have:
        op.create_table("chat_tasks",
            sa.Column("id", BID, primary_key=True, autoincrement=True),
            sa.Column("room_id", BID),
            sa.Column("message_id", BID),
            sa.Column("title", sa.Text()),
            sa.Column("assignee", sa.String()),
            sa.Column("priority", sa.String(), server_default="normal"),
            sa.Column("due_date", sa.Date()),
            sa.Column("status", sa.String(), server_default="open"),
            sa.Column("percent", sa.Integer(), server_default="0"),
            sa.Column("created_by", sa.String()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
        op.create_index("ix_chat_tasks_room", "chat_tasks", ["room_id"])
        op.create_index("ix_chat_tasks_assignee", "chat_tasks", ["assignee"])

    if "chat_announcements" not in have:
        op.create_table("chat_announcements",
            sa.Column("id", BID, primary_key=True, autoincrement=True),
            sa.Column("scope", sa.String(), server_default="company"),
            sa.Column("branch", sa.String()),
            sa.Column("title", sa.String()),
            sa.Column("body", sa.Text()),
            sa.Column("created_by", sa.String()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("active", sa.Boolean(), server_default=sa.text("true")))


def downgrade():
    for t in ("chat_announcements", "chat_tasks", "chat_presence", "chat_reactions",
              "chat_messages", "chat_members", "chat_rooms"):
        op.drop_table(t)
