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


# ------------------------------------------------------------------ customers
def get_customer(db, business_id, company_id=None):
    cid = _cid(db, company_id)
    return (db.query(models.Customer)
            .filter(models.Customer.company_id == cid,
                    models.Customer.id == business_id)
            .first())


def list_customers(db, company_id=None):
    cid = _cid(db, company_id)
    return (db.query(models.Customer)
            .filter(models.Customer.company_id == cid)
            .all())
