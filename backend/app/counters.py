"""Per-company document-number counters (Engineering Phase 4).

Single reusable service for every document type. Replaces timestamp ids
(`PO-<epoch>`), which could collide on `UNIQUE(company_id, id)` when two documents
are created within the same second for one company.

Concurrency guarantee: `next_number` performs one atomic upsert-increment
(`INSERT … ON CONFLICT DO UPDATE … RETURNING`). PostgreSQL serialises concurrent
callers on the counter row (and resolves the first-insert race via ON CONFLICT);
SQLite serialises writers. The increment runs INSIDE the caller's transaction and
is NOT committed here, so the number and the document commit atomically — a rolled-
back document reverts its counter increment (gap-free), while a committed one
advances it. This yields collision-free, per-company, gap-minimal sequences.
"""
from sqlalchemy import text

# Canonical document-type codes. New codes may be added freely; the counter is
# generic. Sales/invoices/receipts/returns/adjustments are ready to adopt this the
# moment those documents gain a visible number.
PURCHASE = "PO"
TRANSFER = "TR"
APPROVAL = "AP"
MOVEMENT = "MV"
SALE = "SALE"
INVOICE = "INV"
RECEIPT = "RCPT"
RETURN = "RET"
ADJUSTMENT = "ADJ"


def _cid(db, company_id=None):
    if company_id is not None:
        return company_id
    try:
        return db.info.get("company_id") or 1
    except Exception:
        return 1


def next_number(db, doc_type, company_id=None, width=6, prefix=None):
    """Return the next `PREFIX-000001`-style number for (company, doc_type).

    Does not commit; the caller commits the document and the counter together.
    """
    cid = _cid(db, company_id)
    n = db.execute(
        text(
            "INSERT INTO document_counters (company_id, doc_type, next_val) "
            "VALUES (:c, :d, 1) "
            "ON CONFLICT (company_id, doc_type) "
            "DO UPDATE SET next_val = document_counters.next_val + 1 "
            "RETURNING next_val"
        ),
        {"c": cid, "d": doc_type},
    ).scalar()
    return f"{prefix or doc_type}-{int(n):0{width}d}"
