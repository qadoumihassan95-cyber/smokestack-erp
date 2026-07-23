"""Wave B / B-B — reusable expand-and-contract for leaf business-number tables.

Every B-B entity table follows the SAME permanent platform key strategy
(Option B): a surrogate ``row_id`` primary key plus a tenant-scoped
``UNIQUE(company_id, id)``. This module encapsulates that transformation so each
table's migration is a two-line call and there is exactly one audited code path —
no second key philosophy can creep in.

Columns assumed on the source table: a ``String`` business key named ``id`` (the
current primary key) and an ``Integer company_id`` (already added in M2).

The functions are dialect-aware and idempotent:
  * PostgreSQL: sequence-backed ``row_id`` default (so new inserts populate it),
    unique indexes built ``CONCURRENTLY`` (no table lock), PK promotion via
    ``USING INDEX`` (no rebuild).
  * SQLite: table rebuild (SQLite cannot alter a primary key in place).
"""
import sqlalchemy as sa


def _cols(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def _idx(insp, table):
    return {i["name"] for i in insp.get_indexes(table)}


def _pk(insp, table):
    return set(insp.get_pk_constraint(table).get("constrained_columns") or [])


def _names(table):
    return (f"{table}_row_id_seq", f"uq_{table}_row_id", f"uq_{table}_company_id")


# ============================================================ EXPAND (additive)
def expand(op, table, extra_cols):
    """Add surrogate row_id + backfill + surrogate-unique + tenant-scoped unique.

    ``extra_cols`` is the ordered list of (name, sqltype) for the SQLite rebuild in
    a downgrade; not needed here but kept symmetric with ``contract``.
    """
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return
    seq, uq_row, uq_biz = _names(table)
    pg = bind.dialect.name == "postgresql"

    if "row_id" not in _cols(insp, table):
        if pg:
            op.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq}")
            op.add_column(table, sa.Column(
                "row_id", sa.BigInteger(),
                server_default=sa.text(f"nextval('{seq}')"), nullable=True))
            op.execute(f"ALTER SEQUENCE {seq} OWNED BY {table}.row_id")
            op.execute(f"UPDATE {table} SET row_id = nextval('{seq}') WHERE row_id IS NULL")
        else:
            with op.batch_alter_table(table) as b:
                b.add_column(sa.Column("row_id", sa.Integer(), nullable=True))
            op.execute(f"UPDATE {table} SET row_id = rowid WHERE row_id IS NULL")

    op.execute(sa.text(f"UPDATE {table} SET company_id = 1 WHERE company_id IS NULL"))

    insp = sa.inspect(bind)
    have = _idx(insp, table)
    if pg:
        with op.get_context().autocommit_block():
            if uq_row not in have:
                op.create_index(uq_row, table, ["row_id"], unique=True,
                                postgresql_concurrently=True)
            if uq_biz not in have:
                op.create_index(uq_biz, table, ["company_id", "id"], unique=True,
                                postgresql_concurrently=True)
    else:
        if uq_row not in have:
            op.create_index(uq_row, table, ["row_id"], unique=True)
        if uq_biz not in have:
            op.create_index(uq_biz, table, ["company_id", "id"], unique=True)


def expand_down(op, table, extra_cols):
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return
    seq, uq_row, uq_biz = _names(table)
    pg = bind.dialect.name == "postgresql"
    have = _idx(insp, table)
    if pg:
        with op.get_context().autocommit_block():
            if uq_biz in have:
                op.drop_index(uq_biz, table_name=table, postgresql_concurrently=True)
            if uq_row in have:
                op.drop_index(uq_row, table_name=table, postgresql_concurrently=True)
        if "row_id" in _cols(insp, table):
            op.drop_column(table, "row_id")
        op.execute(f"DROP SEQUENCE IF EXISTS {seq}")
    else:
        if uq_biz in have:
            op.drop_index(uq_biz, table_name=table)
        if uq_row in have:
            op.drop_index(uq_row, table_name=table)
        if "row_id" in _cols(insp, table):
            with op.batch_alter_table(table) as b:
                b.drop_column("row_id")


# ============================================================ CONTRACT (PK move)
def contract(op, table, extra_cols):
    """Move PK from business ``id`` to surrogate ``row_id``; keep tenant unique.

    ``extra_cols`` = ordered list of ``sa.Column(...)`` for the non-key columns
    (everything except row_id/company_id/id) used only for the SQLite rebuild.
    """
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return
    if _pk(insp, table) == {"row_id"}:
        return
    seq, uq_row, uq_biz = _names(table)
    op.execute(sa.text(f"UPDATE {table} SET company_id = 1 WHERE company_id IS NULL"))

    if bind.dialect.name == "postgresql":
        op.execute(f"ALTER TABLE {table} ALTER COLUMN row_id SET NOT NULL")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN company_id SET NOT NULL")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN id SET NOT NULL")
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {table}_pkey")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {table}_pkey PRIMARY KEY USING INDEX {uq_row}")
    else:
        _sqlite_rebuild(op, table, extra_cols, pk="row_id",
                        cid_nullable=False, keep_biz_unique=True)


def contract_down(op, table, extra_cols):
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return
    if _pk(insp, table) == {"id"}:
        return
    seq, uq_row, uq_biz = _names(table)
    if bind.dialect.name == "postgresql":
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {table}_pkey")
        op.execute(f"CREATE UNIQUE INDEX {uq_row} ON {table} (row_id)")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {table}_pkey PRIMARY KEY (id)")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN company_id DROP NOT NULL")
    else:
        _sqlite_rebuild(op, table, extra_cols, pk="id",
                        cid_nullable=True, keep_biz_unique=True, add_row_unique=True)


def _sqlite_rebuild(op, table, extra_cols, pk, cid_nullable, keep_biz_unique,
                    add_row_unique=False):
    seq, uq_row, uq_biz = _names(table)
    cols = []
    if pk == "row_id":
        cols.append(sa.Column("row_id", sa.Integer(), primary_key=True, autoincrement=True))
        cols.append(sa.Column("company_id", sa.Integer(), nullable=cid_nullable, server_default="1"))
        cols.append(sa.Column("id", sa.String(), nullable=False))
    else:
        cols.append(sa.Column("company_id", sa.Integer(), nullable=cid_nullable, server_default="1"))
        cols.append(sa.Column("id", sa.String(), primary_key=True))
        cols.append(sa.Column("row_id", sa.Integer()))
    cols.extend(extra_cols)
    args = list(cols)
    if keep_biz_unique:
        args.append(sa.UniqueConstraint("company_id", "id", name=uq_biz))
    colnames = ["row_id", "company_id", "id"] + [c.name for c in extra_cols]
    collist = ", ".join(colnames)
    op.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
    op.create_table(table, *args)
    op.execute(f"INSERT INTO {table} ({collist}) SELECT {collist} FROM {table}_old")
    op.execute(f"DROP TABLE {table}_old")
    if add_row_unique:
        op.create_index(uq_row, table, ["row_id"], unique=True)
