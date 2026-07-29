"""Branch display name — human-facing label, decoupled from the internal key.

Revision ID: f1u2v3w4x5y6
Revises: e0t1u2v3w4x5
Create Date: 2026-07-27

Purely additive & reversible. Adds a nullable `branches.display_name` column and backfills the
three known store keys with their business names. The primary key `branches.name` is NEVER touched,
so every relationship, permission mapping, movement, ledger row and historical record is preserved
exactly. Where `display_name` is NULL, the app falls back to `name`.

  Store A -> GM Tobacco Duncanville
  Store B -> GM Tobacco Lancaster
  Store C -> Smoke Depot Waco

The backfill is idempotent (only fills rows whose display_name is still empty) and only affects rows
whose key matches; any other/renamed branch is left untouched.
"""
from alembic import op
import sqlalchemy as sa

revision = "f1u2v3w4x5y6"
down_revision = "e0t1u2v3w4x5"
branch_labels = None
depends_on = None

# Internal key -> business display name. Keys are the untouched PKs.
DISPLAY = {
    "Store A": "GM Tobacco Duncanville",
    "Store B": "GM Tobacco Lancaster",
    "Store C": "Smoke Depot Waco",
}


def _has_col(insp, table, col):
    try:
        return any(c["name"] == col for c in insp.get_columns(table))
    except Exception:
        return False


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "branches" not in set(insp.get_table_names()):
        return  # nothing to do (fresh DB before create_all); seed handles new installs
    if not _has_col(insp, "branches", "display_name"):
        op.add_column("branches", sa.Column("display_name", sa.String(), nullable=True))
    # Idempotent backfill: only set where still empty, and only for matching keys.
    stmt = sa.text(
        "UPDATE branches SET display_name = :dn "
        "WHERE name = :nm AND (display_name IS NULL OR display_name = '')"
    )
    for key, label in DISPLAY.items():
        bind.execute(stmt, {"dn": label, "nm": key})


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "branches" in set(insp.get_table_names()) and _has_col(insp, "branches", "display_name"):
        # SQLite needs batch mode to drop a column; Postgres drops directly.
        with op.batch_alter_table("branches") as batch:
            batch.drop_column("display_name")
