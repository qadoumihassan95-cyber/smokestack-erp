"""Company-scoped configuration accessor (Wave B, group B-A).

`company_settings` moves from a single global `key` namespace to a per-company
`(company_id, key)` key. These helpers read/write by the composite key and work
in BOTH schema states (expand: `key` still PK + composite unique; contract:
`(company_id, key)` is the PK) — so they are safe across the whole migration.

Company resolution: the caller's session company (`db.info["company_id"]`) when
present, else Company #1. Authenticated requests are tagged (M2/M3); untagged
worker/report paths default to Company #1 until those paths are tenantized (M8).
"""
from datetime import datetime, timezone

from . import models


def _cid(db, company_id=None):
    if company_id is not None:
        return company_id
    try:
        return db.info.get("company_id") or 1
    except Exception:
        return 1


def get_setting(db, key, company_id=None):
    cid = _cid(db, company_id)
    return (db.query(models.CompanySetting)
            .filter(models.CompanySetting.company_id == cid,
                    models.CompanySetting.key == key)
            .first())


def get_value(db, key, company_id=None, default=None):
    row = get_setting(db, key, company_id)
    return row.value if row and row.value is not None else default


def set_value(db, key, value, company_id=None, actor=None):
    cid = _cid(db, company_id)
    row = get_setting(db, key, cid)
    if row is None:
        row = models.CompanySetting(key=key, company_id=cid)
        db.add(row)
    row.value = value
    row.updated_by = getattr(actor, "id", None)
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return row
