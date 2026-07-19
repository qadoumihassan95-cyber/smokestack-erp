from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import date, datetime
from ..database import get_db
from .. import models, security as S, permissions as P
from ..schemas import EmployeeIn, EmployeeUpdate

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
