from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from datetime import date, datetime
from ..database import get_db
from .. import models, security as S, permissions as P, tg_caps as C
from ..schemas import EmployeeIn, EmployeeUpdate
import json

router = APIRouter(prefix="/api", tags=["hr"])

def _emp(e):
    # BF-PR-01 / UX-A: `hourly_rate` was accepted on write, stored, and then never
    # serialized back. The web client reads `e.hourly_rate||0`, so every hourly
    # employee rendered as a $0/hour worker while the database held their real rate.
    # It is mapped to `view_payroll` in `S.FINANCIAL_FIELD_PERMS` exactly like
    # `salary`, so `redact_financials` at each call site already withholds it from
    # roles that may not see pay.
    return {"id": e.id, "name": e.name, "branch": e.branch, "title": e.title,
            "pay_type": e.pay_type, "salary": float(e.salary or 0),
            "hourly_rate": float(e.hourly_rate or 0), "active": e.active,
            "sched_start": e.sched_start or "09:00", "sched_end": e.sched_end or "17:00",
            "sched_days": e.sched_days or "Mon-Sat",
            "role": e.role or "employee", "user_id": e.user_id}

@router.get("/employees")
def employees(branch: str = "all", db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    brs = S.resolve_branches(user, db, branch)
    # salary is omitted for roles without view_payroll (SS-H-002).
    return [S.redact_financials(user, _emp(e))
            for e in db.query(models.Employee).filter(models.Employee.branch.in_(brs)).all()]

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
    return S.redact_financials(user, _emp(e))

@router.put("/employees/{eid}")
def update_employee(eid: str, body: EmployeeUpdate, db: Session = Depends(get_db), user: models.User = Depends(S.require("edit_employee"))):
    e = db.get(models.Employee, eid)
    if not e:
        raise HTTPException(404, "Not found")
    S.assert_same_company(user, e)
    # SS-H-006: the caller must be able to reach the employee's CURRENT branch; a
    # reassignment additionally requires the DESTINATION branch. Authorization is
    # never taken from the requester-supplied replacement branch alone.
    dest = getattr(body, "branch", None) or None
    S.assert_object_branch(user, db, e.branch, dest)
    for f in ("name", "branch", "title", "pay_type", "salary", "hourly_rate",
              "sched_start", "sched_end", "sched_days", "role"):
        v = getattr(body, f, None)
        if v is not None:
            setattr(e, f, v)
    db.commit()
    S.audit(db, user, "edit", "employee", eid)
    return S.redact_financials(user, _emp(e))

@router.post("/employees/{eid}/deactivate")
def deactivate(eid: str, db: Session = Depends(get_db), user: models.User = Depends(S.require("deactivate_employee"))):
    e = db.get(models.Employee, eid)
    if not e:
        raise HTTPException(404, "Not found")
    S.assert_same_company(user, e)
    S.assert_object_branch(user, db, e.branch)   # SS-H-006: must hold the record's branch
    e.active = False; db.commit()
    S.audit(db, user, "deactivate", "employee", eid)
    return {"ok": True}

# --- BF-PR-01: which pay types this build can actually compute -----------------
#
# An ALLOW-LIST, not a deny-list on "hourly". `pay_type` is unvalidated end to end:
# `models.py` gives it no enum and no CHECK, `schemas.py` no validator, and
# `add_employee`/`update_employee` write the caller's string straight through. So
# "Hourly", "HOURLY", " hourly ", "commission", "contract", "" and any typo all
# produce exactly the same uncomputable pay while slipping past `== "hourly"`.
# Listing what we CAN compute fails closed on pay types nobody has invented yet.
#
# When the D4 hourly rules are approved, this set grows and `_payroll_figures`
# learns the second formula — one predicate, one call site.
SUPPORTED_PAY_TYPES = frozenset({"salary"})
UNSUPPORTED_PAY_REASON = (
    "pay type is not supported by this build: hourly and other non-salary pay rules "
    "(D4) are not implemented, so no figure can be computed for this employee")


def _pay_type_supported(e) -> bool:
    return str(e.pay_type or "").strip().casefold() in SUPPORTED_PAY_TYPES


def _payroll_figures(start: str, end: str, branch: str, db: Session, user: models.User):
    """Compute the pay run for ``branch`` — a PLAIN function with NO guard of its own.

    SECURITY (SEC HIGH-07). This body used to live directly on the ``GET /payroll``
    endpoint, and ``finalize`` reached the figures by calling that endpoint as an
    ordinary Python function. A FastAPI ``Depends(...)`` is just a default argument
    value: it is resolved by the router on HTTP dispatch and is *silently skipped* by
    a direct call. So ``view_payroll`` — the permission that gates the read — was
    never evaluated on the finalize path, and roles holding ``run_payroll`` without
    ``view_payroll`` (branch_manager, manager) received every employee's name and pay
    from the write endpoint after being refused the exact same figures by the read.

    The fix is to make the seam honest rather than to add a second guard here: an
    UNGUARDED helper that takes an already-authorized ``user``, and each caller
    applying the permission its own surface requires. A helper that quietly enforced
    ``view_payroll`` internally would break finalize for the roles that are supposed
    to be able to run payroll without seeing it.

    Branch scope is NOT part of that split — ``resolve_branches`` below is inside the
    helper and still 403s on a branch the caller does not hold, on both paths.
    """
    # Employee tax has been removed from payroll entirely: net pay equals gross pay,
    # and there is no employee-tax column, card, or line in any output.
    brs = S.resolve_branches(user, db, branch)
    emps = db.query(models.Employee).filter(models.Employee.active == True, models.Employee.branch.in_(brs)).all()
    days = max(1, (date.fromisoformat(end) - date.fromisoformat(start)).days + 1)
    rows, gross, blocked = [], 0, []
    for e in emps:
        if not _pay_type_supported(e):
            # BF-PR-01: OMIT the figure. A `0` here is a claim that this person
            # earned nothing, and it is the claim the ledger acted on.
            blocked.append(e)
            rows.append({"id": e.id, "name": e.name, "branch": e.branch,
                         "pay_type": e.pay_type, "status": "unsupported",
                         "reason": UNSUPPORTED_PAY_REASON})
            continue
        g = round(float(e.salary or 0) * days / 30)
        gross += g
        rows.append({"id": e.id, "name": e.name, "branch": e.branch, "gross": g, "net": g})

    out = {"start": start, "end": end, "rows": rows}
    if blocked:
        # NO total is published — not even a partial one over the computable rows.
        # ERP-Accounting-Auditor's point is decisive: a mixed salaried/hourly branch
        # yields a NONZERO aggregate that silently omits the hourly component, and
        # nothing downstream can tell that apart from a correct total. Absence is the
        # only honest answer, and it also means `finalize` cannot post a figure by
        # accident if this guard is ever removed — `s["gross"]` would raise instead
        # of quietly posting a wrong number.
        out["status"] = "unsupported"
        out["reason"] = UNSUPPORTED_PAY_REASON
        out["unsupported_employees"] = [e.id for e in blocked]
    else:
        out["gross"] = gross
        out["total_cost"] = gross
    return out


@router.get("/payroll")
def payroll(start: str, end: str, branch: str = "all", db: Session = Depends(get_db),
            user: models.User = Depends(S.require("view_payroll"))):
    return _payroll_figures(start, end, branch, db, user)


@router.post("/payroll/finalize")
def finalize(start: str, end: str, branch: str = "all", db: Session = Depends(get_db),
             user: models.User = Depends(S.require("run_payroll"))):
    # BF-PR-02: THE BRANCH THAT IS COMPUTED MUST BE THE BRANCH THAT IS PERSISTED.
    #
    # This used to compute over the whole resolved scope and then post the combined
    # total under `scope[0]`, with `branch="all"` as the DEFAULT — so the ordinary
    # path for a multi-branch owner posted one PayrollRun labelled Store A carrying
    # Store A + B + C. Worse, the natural key is
    # UNIQUE(company_id, branch, period_start, period_end), so a later explicit
    # `branch=Store B` finalize for the same period could not collide with the row
    # labelled Store A and was ACCEPTED — Store B counted twice.
    #
    # Refusing "all" is stricter than picking a branch for the caller, deliberately:
    # the branch a payroll is attributed to is an accounting fact, and the server
    # must not choose it. This is checked BEFORE any computation, so a refusal reads
    # nothing and writes nothing.
    req = str(branch or "").strip()
    if req == "" or req.casefold() == "all":
        raise HTTPException(400, (
            "Finalizing payroll requires one explicit branch. A pay run is posted "
            "and audited against a single branch, so the branch cannot be defaulted "
            "or combined; finalize each branch separately."))

    # 403s on a branch the caller does not hold. Ordering matters: authorization owes
    # a 403, and it must not be swallowed by the 400 above or below.
    scope = S.resolve_branches(user, db, branch)
    if len(scope) != 1:
        # Defense in depth, not a currently reachable path: `resolve_branches`
        # returns exactly `[req]` for an explicit permitted branch. Stated as a
        # refusal rather than a comment so that a future change to that function
        # cannot silently restore combined posting.
        raise HTTPException(400, "Payroll must resolve to exactly one branch.")
    tgt = scope[0]

    s = _payroll_figures(start, end, tgt, db, user)

    # BF-PR-01: refuse to POST a figure this build cannot COMPUTE.
    #
    # Placed before the first `db.add`, so ledger, payroll_runs and audit_log are
    # untouched BY CONSTRUCTION rather than by cleanup. 409, not 422: the request is
    # well-formed, it is the data state that makes it unprocessable.
    #
    # The message names the blocking employees and their pay types — both already
    # readable by any role that can reach this endpoint, via `GET /api/employees` —
    # and deliberately names NO money, because `run_payroll` does not imply
    # `view_payroll` and a diagnostic must not become a side channel around that.
    if s.get("status") == "unsupported":
        blocked = ", ".join(f"{e['id']} ({e.get('pay_type') or 'unset'})"
                            for e in s["rows"] if e.get("status") == "unsupported")
        raise HTTPException(409, (
            f"Payroll for {tgt} cannot be finalized: {blocked}. "
            f"{UNSUPPORTED_PAY_REASON}. Approved D4 pay rules are required before "
            f"these employees can be paid through payroll."))

    # SIM-06 + AA-06: the posting, its natural key and the audit evidence are one
    # transaction, and the pay period's uniqueness is enforced by the DATABASE.
    # An application "has it been finalized?" pre-check cannot hold under
    # concurrency: two simultaneous requests both read "no" and both insert.
    row = models.Ledger(branch=tgt, type="payroll", amount=s["gross"],
                        memo=f"Payroll {start}->{end}", created_by=user.id)
    db.add(row)
    db.flush()
    db.add(models.PayrollRun(branch=tgt, period_start=date.fromisoformat(start),
                             period_end=date.fromisoformat(end), gross=s["gross"],
                             ledger_id=row.id, finalized_by=user.id))
    S.audit(db, user, "finalize", "payroll", f"{start}_{end}", f"gross {s['gross']}",
            commit=False)
    try:
        db.commit()
    except IntegrityError:
        # The unique constraint rejected a duplicate period. Nothing was written:
        # the ledger posting, the run row and the audit row roll back together.
        db.rollback()
        raise HTTPException(409, f"Payroll for {start}->{end} at {tgt} has already been finalized.")

    # SECURITY (SEC HIGH-07): the pay run posted, but WHAT WE SAY BACK is a read, and
    # a read of payroll needs ``view_payroll``. A caller holding only ``run_payroll``
    # gets the receipt for the action they performed and nothing about who was paid
    # what. The gate is on the WHOLE payload, deliberately, and NOT via
    # ``S.redact_financials``: that helper strips keys by NAME, and these figures are
    # keyed ``gross``/``net`` while the map carries ``gross_pay``/``net_pay``. Wrapping
    # this return in it would remove nothing at all while reading like a fix.
    #
    # Nor are those two names simply added to the map: ``net`` is ALSO the key for
    # "net operating result" in reports_tg.py, a profit figure, so mapping it to
    # ``view_payroll`` would mislabel a different domain. That ambiguity is the point —
    # a redaction list keyed by field NAME is a denylist, and it protects only the
    # spellings someone remembered. The whole-payload gate below needs no vocabulary
    # and holds for keys nobody has added yet.
    receipt = {"ok": True, "start": start, "end": end, "branch": tgt}
    if not P.can(user.role, "view_payroll"):
        return receipt
    return {**receipt, **s}


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
    S.assert_same_company(user, e)
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
    S.assert_same_company(user, e)
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
