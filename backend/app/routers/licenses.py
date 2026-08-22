"""Licenses & official documents — business/tobacco/tax permits, inspections,
insurance, leases, etc., with expiration tracking and alert buckets
(90 / 60 / 30 / 7 days before expiry, and expired). Reuses branch scoping,
permissions and the audit log."""
from datetime import date, datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, security as S
from ..schemas import LicenseIn

router = APIRouter(prefix="/api/licenses", tags=["licenses"])


def _pdate(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s)[:10]).date()
    except Exception:  # noqa: BLE001
        return None


def _bucket(days):
    """Alert bucket for a days-to-expiry value."""
    if days is None:
        return "unknown"
    if days < 0:
        return "expired"
    if days <= 7:
        return "d7"
    if days <= 30:
        return "d30"
    if days <= 60:
        return "d60"
    if days <= 90:
        return "d90"
    return "ok"


def _status(days):
    if days is None:
        return "active"
    if days < 0:
        return "expired"
    if days <= 90:
        return "expiring"
    return "active"


def _serialize(x):
    exp = x.expiry_date
    days = (exp - date.today()).days if exp else None
    return {"id": x.id, "name": x.name, "doc_type": x.doc_type, "branch": x.branch,
            "doc_number": x.doc_number, "authority": x.authority,
            "issue_date": str(x.issue_date) if x.issue_date else None,
            "expiry_date": str(exp) if exp else None,
            "days_to_expiry": days, "bucket": _bucket(days), "status": _status(days),
            "responsible": x.responsible, "notes": x.notes, "attachment": x.attachment}


@router.get("")
def list_licenses(branch: str = "all", db: Session = Depends(get_db),
                  user: models.User = Depends(S.require("view"))):
    brs = S.resolve_branches(user, db, branch)
    rows = (db.query(models.License)
            .filter((models.License.branch.in_(brs)) | (models.License.branch.is_(None)))
            .order_by(models.License.expiry_date.asc().nullslast()).all())
    return [_serialize(x) for x in rows]


@router.get("/alerts")
def alerts(db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    """Expiring/expired documents grouped by urgency — for dashboard + Telegram alerts."""
    brs = S.scope_branches(user, db)
    rows = (db.query(models.License)
            .filter((models.License.branch.in_(brs)) | (models.License.branch.is_(None))).all())
    items = [_serialize(x) for x in rows]
    alerting = [i for i in items if i["bucket"] in ("expired", "d7", "d30", "d60", "d90")]
    alerting.sort(key=lambda i: (i["days_to_expiry"] is None, i["days_to_expiry"]))
    return {"count": len(alerting),
            "expired": sum(1 for i in alerting if i["bucket"] == "expired"),
            "critical": sum(1 for i in alerting if i["bucket"] in ("d7", "d30")),
            "items": alerting}


@router.post("", status_code=201)
def create_license(body: LicenseIn, db: Session = Depends(get_db),
                   user: models.User = Depends(S.require("create"))):
    if body.branch:
        S.assert_branch(user, db, body.branch)
    x = models.License(name=body.name, doc_type=body.doc_type, branch=body.branch,
                       doc_number=body.doc_number, authority=body.authority,
                       issue_date=_pdate(body.issue_date), expiry_date=_pdate(body.expiry_date),
                       responsible=body.responsible, notes=body.notes, attachment=body.attachment,
                       created_by=user.id)
    db.add(x); db.commit()
    S.audit(db, user, "create", "license", x.id, f"{body.name} exp {body.expiry_date}")
    return _serialize(x)


@router.put("/{lid}")
def update_license(lid: int, body: LicenseIn, db: Session = Depends(get_db),
                   user: models.User = Depends(S.require("edit"))):
    x = db.get(models.License, lid)
    if not x:
        raise HTTPException(404, "Not found")
    # SECURITY (SEC-17): this guarded the branch the CALLER SUPPLIED and never the
    # branch the record is actually in — so omitting `branch` from the body ran no
    # branch check at all, and a branch-scoped role could rewrite another branch's
    # regulatory documents (permit numbers, expiry dates). Because `branch` is also
    # in the setattr allow-list below, it could additionally RELOCATE another
    # branch's licence into its own.
    #
    # `security.py:212` already states the rule this violated — "Authorization is
    # never derived solely from a branch supplied by the requester" — and `hr.py`
    # already obeys it with the same helper. This is a port, not a design.
    S.assert_object_branch(user, db, x.branch, body.branch or None)
    for f in ("name", "doc_type", "branch", "doc_number", "authority", "responsible", "notes", "attachment"):
        v = getattr(body, f, None)
        if v is not None:
            setattr(x, f, v)
    if body.issue_date is not None:
        x.issue_date = _pdate(body.issue_date)
    if body.expiry_date is not None:
        x.expiry_date = _pdate(body.expiry_date)
    db.commit()
    S.audit(db, user, "edit", "license", lid)
    return _serialize(x)


@router.delete("/{lid}")
def delete_license(lid: int, db: Session = Depends(get_db),
                   user: models.User = Depends(S.require("delete"))):
    x = db.get(models.License, lid)
    if not x:
        raise HTTPException(404, "Not found")
    # SECURITY (SEC-17, latent sibling): this had NO branch check of any kind. It
    # was not exploitable at 8868af8 only because `delete` happens to be held by
    # owner and admin alone, both all-branch — i.e. it was safe by the current
    # permission matrix, not by construction. Granting `delete` to any
    # branch-scoped role would have turned it into cross-branch DELETION of
    # compliance records, with no code change and no failing test. Fixed in the
    # same edit as the reported route rather than deferred, because "safe under
    # today's config" is exactly the state that stops being true silently.
    S.assert_object_branch(user, db, x.branch)
    db.delete(x); db.commit()
    S.audit(db, user, "delete", "license", lid, x.name)
    return {"ok": True}
