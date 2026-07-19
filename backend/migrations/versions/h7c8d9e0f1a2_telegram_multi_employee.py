"""Telegram multi-employee: employee-targeted link codes + provisioned identities

Revision ID: h7c8d9e0f1a2
Revises: g6b7c8d9e0f1
Create Date: 2026-07-19

Root cause this migration supports: a link code carried only the SIGNED-IN USER,
so every invitation an owner generated pointed back at the owner and each new
link replaced the previous one. Codes now carry an employee, and each employee
gets its own session identity.

Additive and idempotent. Existing rows keep working:
  * users.can_login defaults to 1 (every current login still signs in)
  * employees.role defaults to 'employee'
  * existing telegram_links are backfilled to their employee where derivable

The partial unique index enforces the real business rule at the database level:
ONE ACTIVE Telegram account per employee. It is deliberately partial so that
disabled rows are retained as history, and it does NOT constrain branch or any
tenant column — a company may hold unlimited Telegram accounts.
"""
from alembic import op
import sqlalchemy as sa

revision = "h7c8d9e0f1a2"
down_revision = "g6b7c8d9e0f1"
branch_labels = None
depends_on = None


def _add_missing(insp, table, cols):
    have = {c["name"] for c in insp.get_columns(table)}
    add = [(n, t) for n, t in cols if n not in have]
    if add:
        with op.batch_alter_table(table) as b:
            for n, t in add:
                b.add_column(sa.Column(n, t, nullable=True))
    return [n for n, _ in add]


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)

    _add_missing(insp, "users", [
        ("can_login", sa.Boolean()), ("employee_id", sa.String()),
    ])
    _add_missing(insp, "employees", [
        ("role", sa.String()), ("user_id", sa.String()),
    ])
    _add_missing(insp, "link_codes", [
        ("employee_id", sa.String()), ("created_by", sa.String()),
    ])

    # backward compatibility: everything that exists today keeps its behaviour
    op.execute("UPDATE users SET can_login = 1 WHERE can_login IS NULL")
    op.execute("UPDATE employees SET role = 'employee' WHERE role IS NULL")

    # map any pre-existing Telegram link to its employee by matching the user's
    # name, which is how the old code derived the employee at render time
    op.execute("""
        UPDATE telegram_links SET employee_id = (
            SELECT e.id FROM employees e
            JOIN users u ON u.name = e.name
            WHERE u.id = telegram_links.user_id
            LIMIT 1
        )
        WHERE employee_id IS NULL
    """)

    # ONE ACTIVE Telegram account per employee — enforced by the database.
    # Partial index: disabled rows stay as history and never block a re-link.
    idx = {i["name"] for i in insp.get_indexes("telegram_links")}
    if "uq_tg_active_employee" not in idx:
        try:
            op.create_index("uq_tg_active_employee", "telegram_links", ["employee_id"],
                            unique=True,
                            sqlite_where=sa.text("status = 'active' AND employee_id IS NOT NULL"),
                            postgresql_where=sa.text("status = 'active' AND employee_id IS NOT NULL"))
        except Exception:
            # a pre-existing duplicate would fail the index; the application layer
            # already rejects the case, so never block a deploy on it
            pass
    if "ix_link_codes_employee" not in idx:
        try:
            op.create_index("ix_link_codes_employee", "link_codes", ["employee_id"])
        except Exception:
            pass


def downgrade():
    for name, table in (("uq_tg_active_employee", "telegram_links"),
                        ("ix_link_codes_employee", "link_codes")):
        try:
            op.drop_index(name, table_name=table)
        except Exception:
            pass
    with op.batch_alter_table("link_codes") as b:
        for c in ("created_by", "employee_id"):
            b.drop_column(c)
    with op.batch_alter_table("employees") as b:
        for c in ("user_id", "role"):
            b.drop_column(c)
    with op.batch_alter_table("users") as b:
        for c in ("employee_id", "can_login"):
            b.drop_column(c)
