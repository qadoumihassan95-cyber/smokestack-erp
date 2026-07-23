"""Company-scoped accessors for business-number entities (Wave B, group B-B).

These tables move from a global natural key (`id`) to a surrogate `row_id`
primary key plus a tenant-scoped business key `UNIQUE(company_id, id)`. Lookups
must therefore resolve by `(company_id, id)` rather than by the primary key, so
they work in BOTH schema states (EXPAND: `id` is still the PK; CONTRACT: `row_id`
is the PK). This mirrors `company_config` and is the reusable B-B pattern.

Company resolution: the caller's session company (`db.info["company_id"]`) when
present, else Company #1 — matching the fail-closed tenancy layer.
"""
from . import models


def _cid(db, company_id=None):
    if company_id is not None:
        return company_id
    try:
        return db.info.get("company_id") or 1
    except Exception:
        return 1


# ---------------------------------------------------------- generic (Option B)
def get_by_business_id(db, model, business_id, company_id=None):
    """Resolve a B-B entity by its tenant-scoped business number (company_id, id).
    Works in both schema phases (EXPAND: id is the PK; CONTRACT: row_id is)."""
    cid = _cid(db, company_id)
    return (db.query(model)
            .filter(model.company_id == cid, model.id == business_id)
            .first())


def list_for_company(db, model, company_id=None):
    cid = _cid(db, company_id)
    return db.query(model).filter(model.company_id == cid).all()


# ------------------------------------------------------------------ customers
def get_customer(db, business_id, company_id=None):
    return get_by_business_id(db, models.Customer, business_id, company_id)


def list_customers(db, company_id=None):
    return list_for_company(db, models.Customer, company_id)


# ------------------------------------------------------------------ suppliers
def get_supplier(db, business_id, company_id=None):
    return get_by_business_id(db, models.Supplier, business_id, company_id)


def list_suppliers(db, company_id=None):
    return list_for_company(db, models.Supplier, company_id)
