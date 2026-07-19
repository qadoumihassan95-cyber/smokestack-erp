from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import date, datetime
from ..database import get_db
from .. import models, security as S, permissions as P, tg_caps as C
from ..schemas import EmployeeIn, EmployeeUpdate
import json

router = APIRouter(prefix="/api", tags=["hr"])

def _emp(e):
    return {"id": e.id, "name": e.name, "branch": e.branch, "title": e.title,
            "pay_type": e.pay_type, "salary": float(e.salary or 0), "active": e.active,
            "sched_start": e.sched_start or "09:00", "sched_end": e.sched_end or "17:00",
            "sched_days": e.sched_days or "Mon-Sat",
            "role": e.role or "employee", "user_id": e.user_id}

@router.get("/employees")
def employees(branch: str = "all", db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    brs = S.scope_branches(user, db) if branch == "all" else [branch]
    return [_emp(e) for e in db.query(models.Employee).filter(models.Employee.branch.in_(brs)).all()]

@router.post("/employees", status_code=201)
def add_employee(body: EmployeeIn, db: Session = Depends(get_db), user: models.User = Depends(S.require("add_employee"))):
    S.assert_branch(user, db, body.branch)
    if db.get(models.Employee, body.id):
        raise HTTPException(409, "Employee ID already exists")
    e = models.Employee(id=body.id, name=body.name, branch=body.branch, title=body.title,
                        pay_type=body.pay_type, salary=body.salary, hourly_rate=body.hourly_rate,
                        sched_start=body.sched_start or "09:00", sched_end=body.sched_end or "17:00",
                        sched_days=body.sched_days or "Mon-Sat", active=True,
                        role=(getattr(body, "role", None) or "employee"), created_by=user.id)
    db.add(e); db.commit()
    S.audit(db, user, "create", "employee", e.id, f"{e.name} @ {e.branch}")
    return _emp(e)

@router.put("/employees/{eid}")
def update_employee(eid: str, body: EmployeeUpdate, db: Session = Depends(get_db), user: models.User = Depends(S.require("edit_employee"))):
    e = db.get(models.Employee, eid)
    if not e:
        raise HTTPException(404, "Not found")
    # Branch permission checked against the target branch (new if changing, else current).
    S.assert_branch(user, db, body.branch or e.branch)
    for f in ("name", "branch", "title", "pay_type", "salary", "hourly_rate",
              "sched_start", "sched_end", "sched_days", "role"):
        v = getattr(body, f, None)
        if v is not None:
            setattr(e, f, v)
    db.commit()
    S.audit(db, user, "edit", "employee", eid)
    return _emp(e)

@router.post("/employees/{eid}/deactivate")
def deactivate(eid: str, db: Session = Depends(get_db), user: models.User = Depends(S.require("deactivate_employee"))):
    e = db.get(models.Employee, eid)
    if not e:
        raise HTTPException(404, "Not found")
    e.active = False; db.commit()
    S.audit(db, user, "deactivate", "employee", eid)
    return {"ok": True}

@router.get("/payroll")
def payroll(start: str, end: str, branch: str = "all", db: Session = Depends(get_db),
            user: models.User = Depends(S.require("view_payroll"))):
    # Employee tax has been removed from payroll entirely: net pay equals gross pay,
    # and there is no employee-tax column, card, or line in any output.
    brs = S.scope_branches(user, db) if branch == "all" else [branch]
    emps = db.query(models.Employee).filter(models.Employee.active == True, models.Employee.branch.in_(brs)).all()
    days = max(1, (date.fromisoformat(end) - date.fromisoformat(start)).days + 1)
    rows, gross = [], 0
    for e in emps:
        g = round(float(e.salary or 0) * days / 30)
        gross += g
        rows.append({"name": e.name, "branch": e.branch, "gross": g, "net": g})
    return {"start": start, "end": end, "rows": rows, "gross": gross, "total_cost": gross}

@router.post("/payroll/finalize")
def finalize(start: str, end: str, branch: str = "all", db: Session = Depends(get_db),
             user: models.User = Depends(S.require("run_payroll"))):
    s = payroll(start, end, branch, db, user)
    tgt = branch if branch != "all" else S.scope_branches(user, db)[0]
    db.add(models.Ledger(branch=tgt, type="payroll", amount=s["gross"],
                         memo=f"Payroll {start}->{end}", created_by=user.id))
    db.commit()
    S.audit(db, user, "finalize", "payroll", f"{start}_{end}", f"gross {s['gross']}")
    return {"ok": True, **s}


# ---------------------------------------------------------------------------
# Telegram Permissions — the admin interface for capability toggles.
# No code edit is needed to change what an employee may do from Telegram.
# ---------------------------------------------------------------------------

def _tg_overrides(e):
    if not e.tg_perms:
        return {}
    try:
        v = json.loads(e.tg_perms)
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


@router.get("/employees/{eid}/telegram-permissions")
def get_tg_perms(eid: str, db: Session = Depends(get_db),
                 user: models.User = Depends(S.require("view"))):
    e = db.get(models.Employee, eid)
    if not e:
        raise HTTPException(404, "Not found")
    S.assert_branch(user, db, e.branch)
    role = e.role or "employee"
    link = (db.query(models.TelegramLink)
            .filter(models.TelegramLink.employee_id == eid,
                    models.TelegramLink.status == "active").first())
    return {"employee_id": e.id, "employee": e.name, "role": role, "branch": e.branch,
            "linked": bool(link), "tg_id": (link.tg_id if link else None),
            "tg_username": (link.username if link else None),
            "capabilities": C.describe(role, _tg_overrides(e), P),
            "editable": P.can(user.role, "manage_permissions")}


@router.put("/employees/{eid}/telegram-permissions")
def set_tg_perms(eid: str, body: dict, db: Session = Depends(get_db),
                 user: models.User = Depends(S.require("manage_permissions"))):
    """Owner switches individual Telegram capabilities on or off.

    A capability the employee's ROLE does not grant can never be switched on —
    the ERP permission map stays the ceiling.
    """
    e = db.get(models.Employee, eid)
    if not e:
        raise HTTPException(404, "Not found")
    S.assert_branch(user, db, e.branch)
    role = e.role or "employee"
    incoming = body.get("capabilities") or {}
    if not isinstance(incoming, dict):
        raise HTTPException(422, "capabilities must be an object of {key: bool}")

    cleaned, rejected = {}, []
    for k, v in incoming.items():
        if k not in C.CAP_KEYS:
            raise HTTPException(422, f"Unknown capability: {k}")
        if bool(v) and not C.role_allows(role, k, P):
            rejected.append(k)          # cannot exceed the role
            cleaned[k] = False
        else:
            cleaned[k] = bool(v)
    e.tg_perms = json.dumps(cleaned)
    db.commit()
    S.audit(db, user, "set_telegram_permissions", "employee", eid,
            detail=", ".join(f"{k}={'on' if v else 'off'}" for k, v in sorted(cleaned.items())))
    out = {"employee_id": e.id, "role": role,
           "capabilities": C.describe(role, cleaned, P)}
    if rejected:
        out["rejected"] = rejected
        out["note"] = ("These capabilities are not granted by the employee's role "
                       "and were left off: " + ", ".join(C.CAP_LABEL[k] for k in rejected))
    return out


@router.get("/telegram-capabilities")
def capability_catalogue(user: models.User = Depends(S.require("view"))):
    """The catalogue itself, so the UI never hard-codes the list."""
    return [{"key": k, "label": C.CAP_LABEL[k], "requires": C.CAP_PERMS[k]} for k in C.CAP_KEYS]
