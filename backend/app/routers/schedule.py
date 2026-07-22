"""Employee Work Schedule — weekly scheduling calendar + per-employee Telegram
delivery, integrated with the existing Employees module and Telegram links.

RBAC: `manage_schedules` (owner / admin / branch_manager / manager) may create,
edit, delete, publish and send schedules; everyone else is read-only and can
only ever see their OWN schedule (via /my, or a self-scoped /entries).

Delivery reuses the same idempotency-ledger + worker-loop pattern as the
scheduled reports and reminders: rows are queued into telegram_delivery_log with
a UNIQUE idem_key and the worker drains them, so a restart or a second worker
instance can never double-send. No AI, no external calendar service.
"""
import json
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session

from ..database import get_db
from ..config import settings
from .. import models, security as S, permissions as P
from .. import schedule_tg as SC
from .. import reports_tg as R

router = APIRouter(prefix="/api/schedule", tags=["schedule"])

WEEKLY_SEND_HOUR = 18   # business-local hour on the day before the new week


# ------------------------------------------------------------------ helpers
def _is_manager(user):
    return P.can(user.role, "manage_schedules")


def _emp_name(db, employee_id):
    e = db.get(models.Employee, employee_id)
    return e.name if e else employee_id


def _employees_in_scope(db, user):
    brs = set(S.scope_branches(user, db))
    q = db.query(models.Employee).filter(models.Employee.active == True)  # noqa: E712
    return [e for e in q.all() if (P.can_see_all(user.role) or e.branch in brs)]


def _entry_out(r):
    return {"id": r.id, "employee_id": r.employee_id, "branch": r.branch,
            "work_date": str(r.work_date), "week_start": str(r.week_start),
            "start_time": r.start_time, "end_time": r.end_time,
            "break_minutes": r.break_minutes or 0, "notes": r.notes or "",
            "is_off": bool(r.is_off), "published": bool(r.published)}


def _upsert(db, employee_id, branch, d, fields, actor, template_id=None):
    """Insert or replace one employee's shift on a given date."""
    ws = SC.week_start_of(d)
    row = (db.query(models.EmployeeSchedule)
           .filter(models.EmployeeSchedule.employee_id == employee_id,
                   models.EmployeeSchedule.work_date == d).first())
    if not row:
        row = models.EmployeeSchedule(employee_id=employee_id, work_date=d,
                                      created_by=getattr(actor, "id", None))
        db.add(row)
    row.branch = branch
    row.week_start = ws
    row.start_time = fields.get("start_time", "09:00")
    row.end_time = fields.get("end_time", "17:00")
    row.break_minutes = int(fields.get("break_minutes", 0) or 0)
    row.notes = fields.get("notes")
    row.is_off = bool(fields.get("is_off", False))
    row.template_id = template_id
    row.published = False
    row.updated_at = datetime.now(timezone.utc)
    return row


def _queue(db, emp, week_start, kind, actor, message=None, unique_suffix=None):
    """Queue one schedule delivery for an employee. Returns the row, a 'skipped'
    row when the employee has no active Telegram link, or None on idem clash."""
    link = SC.employee_tg(db, emp.id)
    msg = message if message is not None else SC.render_week_message(db, emp, week_start)
    if not link:
        key = f"{kind}|{week_start}|noemp|{emp.id}|{unique_suffix or ''}"
        if db.query(models.TelegramDeliveryLog).filter_by(idem_key=key).first():
            return None
        row = models.TelegramDeliveryLog(idem_key=key, employee_id=emp.id, tg_id=None,
                                         recipient=emp.name, week_start=week_start, kind=kind,
                                         status="skipped", message=msg,
                                         error="no linked Telegram account",
                                         created_by=getattr(actor, "id", None))
        db.add(row)
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback(); return None
        return row
    suffix = unique_suffix if unique_suffix is not None else datetime.now(timezone.utc).strftime("%H%M%S%f")
    key = (f"weekly|{week_start}|{link.tg_id}" if kind == "weekly"
           else f"{kind}|{week_start}|{link.tg_id}|{suffix}")
    if db.query(models.TelegramDeliveryLog).filter_by(idem_key=key).first():
        return None
    row = models.TelegramDeliveryLog(idem_key=key, employee_id=emp.id, tg_id=link.tg_id,
                                     recipient=emp.name, week_start=week_start, kind=kind,
                                     status="queued", message=msg,
                                     created_by=getattr(actor, "id", None))
    db.add(row)
    try:
        db.commit()
    except Exception:  # noqa: BLE001  (another writer won the idem race)
        db.rollback(); return None
    return row


def _bot(x_bot_token):
    if not settings.bot_token or x_bot_token != settings.bot_token:
        raise HTTPException(403, "Forbidden")


# ------------------------------------------------------------------ calendar
@router.get("/entries")
def entries(start: str = "", end: str = "", employee_id: str = "", branch: str = "",
            db: Session = Depends(get_db),
            user: models.User = Depends(S.require("view"))):
    """Calendar data for a date range. Managers see their branch scope; everyone
    else is silently restricted to their own schedule."""
    try:
        d0 = date.fromisoformat(start) if start else date.today() - timedelta(days=date.today().weekday())
        d1 = date.fromisoformat(end) if end else d0 + timedelta(days=41)
    except ValueError:
        raise HTTPException(422, "start/end must be YYYY-MM-DD")
    q = (db.query(models.EmployeeSchedule)
         .filter(models.EmployeeSchedule.work_date >= d0,
                 models.EmployeeSchedule.work_date <= d1))
    if not _is_manager(user):
        emp = SC.employee_of_user(db, user)
        if not emp:
            return {"entries": [], "can_manage": False, "self_only": True}
        q = q.filter(models.EmployeeSchedule.employee_id == emp.id)
    else:
        brs = set(S.scope_branches(user, db))
        if not P.can_see_all(user.role):
            q = q.filter(models.EmployeeSchedule.branch.in_(brs or {"__none__"}))
        if employee_id:
            q = q.filter(models.EmployeeSchedule.employee_id == employee_id)
        if branch:
            q = q.filter(models.EmployeeSchedule.branch == branch)
    rows = q.order_by(models.EmployeeSchedule.work_date.asc()).all()
    emps = [{"id": e.id, "name": e.name, "branch": e.branch, "role": e.role,
             "tg_linked": bool(SC.employee_tg(db, e.id))}
            for e in (_employees_in_scope(db, user) if _is_manager(user) else [])]
    return {"entries": [_entry_out(r) for r in rows], "employees": emps,
            "can_manage": _is_manager(user), "self_only": not _is_manager(user)}


@router.get("/my")
def my_schedule(start: str = "", end: str = "", db: Session = Depends(get_db),
                user: models.User = Depends(S.require("view"))):
    """The signed-in employee's own schedule — read only."""
    emp = SC.employee_of_user(db, user)
    if not emp:
        return {"employee": None, "entries": [], "week_message": None}
    try:
        d0 = date.fromisoformat(start) if start else SC.week_start_of(date.today())
        d1 = date.fromisoformat(end) if end else d0 + timedelta(days=41)
    except ValueError:
        raise HTTPException(422, "start/end must be YYYY-MM-DD")
    rows = (db.query(models.EmployeeSchedule)
            .filter(models.EmployeeSchedule.employee_id == emp.id,
                    models.EmployeeSchedule.work_date >= d0,
                    models.EmployeeSchedule.work_date <= d1)
            .order_by(models.EmployeeSchedule.work_date.asc()).all())
    ws = SC.week_start_of(date.today())
    return {"employee": {"id": emp.id, "name": emp.name, "branch": emp.branch},
            "entries": [_entry_out(r) for r in rows],
            "week_message": SC.render_week_message(db, emp, ws)}


# ------------------------------------------------------------------ create/edit
@router.post("/entries", status_code=201)
def create_entries(body: dict, db: Session = Depends(get_db),
                   user: models.User = Depends(S.require("manage_schedules"))):
    """Assign shifts. Accepts explicit `dates`, or a recurring pattern
    (recurrence + weekdays + weeks) that is expanded into concrete dates."""
    body = body or {}
    employee_id = str(body.get("employee_id") or "")
    emp = db.get(models.Employee, employee_id)
    if not emp:
        raise HTTPException(404, "Employee not found")
    branch = body.get("branch") or emp.branch
    if branch:
        S.assert_branch(user, db, branch)
    if body.get("dates"):
        dates = [date.fromisoformat(x) for x in body["dates"]]
    else:
        dates = SC.expand_dates(body.get("start") or str(date.today()),
                                body.get("weeks", 1), body.get("recurrence", "weekly"),
                                body.get("weekdays"), bool(body.get("every_other")))
    if not dates:
        raise HTTPException(422, "No dates resolved from the pattern")
    fields = {k: body.get(k) for k in ("start_time", "end_time", "break_minutes", "notes", "is_off")}
    fields.setdefault("start_time", "09:00")
    fields.setdefault("end_time", "17:00")
    made = [_upsert(db, employee_id, branch, d, fields, user, body.get("template_id")) for d in dates]
    db.commit()
    S.audit(db, user, "schedule_created", "schedule", employee_id,
            detail=f"{len(made)} day(s) {dates[0]}..{dates[-1]}")
    return {"created": [_entry_out(r) for r in made]}


@router.post("/bulk", status_code=201)
def bulk_assign(body: dict, db: Session = Depends(get_db),
                user: models.User = Depends(S.require("manage_schedules"))):
    """Apply one pattern to many employees at once."""
    body = body or {}
    ids = [str(x) for x in (body.get("employee_ids") or [])]
    if not ids:
        raise HTTPException(422, "employee_ids required")
    dates_for = lambda: SC.expand_dates(body.get("start") or str(date.today()),  # noqa: E731
                                        body.get("weeks", 1), body.get("recurrence", "weekly"),
                                        body.get("weekdays"), bool(body.get("every_other")))
    fields = {k: body.get(k) for k in ("start_time", "end_time", "break_minutes", "notes", "is_off")}
    fields.setdefault("start_time", "09:00"); fields.setdefault("end_time", "17:00")
    n = 0
    for eid in ids:
        emp = db.get(models.Employee, eid)
        if not emp:
            continue
        branch = body.get("branch") or emp.branch
        if branch and branch not in S.scope_branches(user, db) and not P.can_see_all(user.role):
            continue
        for d in dates_for():
            _upsert(db, eid, branch, d, fields, user)
            n += 1
    db.commit()
    S.audit(db, user, "schedule_created", "schedule", ",".join(ids), detail=f"bulk {n} shifts")
    return {"created": n, "employees": len(ids)}


@router.put("/entries/{eid}")
def edit_entry(eid: int, body: dict, db: Session = Depends(get_db),
               user: models.User = Depends(S.require("manage_schedules"))):
    row = db.get(models.EmployeeSchedule, eid)
    if not row:
        raise HTTPException(404, "Entry not found")
    if not P.can_see_all(user.role):
        S.assert_branch(user, db, row.branch)
    for f in ("start_time", "end_time", "notes"):
        if f in body:
            setattr(row, f, body[f])
    if "break_minutes" in body:
        row.break_minutes = int(body["break_minutes"] or 0)
    if "is_off" in body:
        row.is_off = bool(body["is_off"])
    if "branch" in body and body["branch"]:
        S.assert_branch(user, db, body["branch"]); row.branch = body["branch"]
    row.updated_at = datetime.now(timezone.utc)
    was_published = row.published
    db.commit()
    S.audit(db, user, "schedule_edited", "schedule", row.employee_id, detail=str(row.work_date))
    # a change to an already-published week re-notifies that employee
    if was_published:
        emp = db.get(models.Employee, row.employee_id)
        if emp:
            _queue(db, emp, row.week_start, "update", user)
    return _entry_out(row)


@router.delete("/entries/{eid}")
def delete_entry(eid: int, db: Session = Depends(get_db),
                 user: models.User = Depends(S.require("manage_schedules"))):
    row = db.get(models.EmployeeSchedule, eid)
    if not row:
        raise HTTPException(404, "Entry not found")
    if not P.can_see_all(user.role):
        S.assert_branch(user, db, row.branch)
    emp_id, wd = row.employee_id, str(row.work_date)
    db.delete(row); db.commit()
    S.audit(db, user, "schedule_deleted", "schedule", emp_id, detail=wd)
    return {"ok": True}


@router.post("/copy-week")
def copy_week(body: dict, db: Session = Depends(get_db),
              user: models.User = Depends(S.require("manage_schedules"))):
    """Duplicate one week's shifts to one or more future weeks. Also powers
    'duplicate previous week' (from_week = last week, to_weeks = [this week])."""
    body = body or {}
    from_ws = SC.week_start_of(body.get("from_week") or str(date.today()))
    to_weeks = [SC.week_start_of(x) for x in (body.get("to_weeks") or [])]
    if not to_weeks:
        raise HTTPException(422, "to_weeks required")
    q = db.query(models.EmployeeSchedule).filter(models.EmployeeSchedule.week_start == from_ws)
    if body.get("employee_id"):
        q = q.filter(models.EmployeeSchedule.employee_id == body["employee_id"])
    src = q.all()
    n = 0
    for r in src:
        if not P.can_see_all(user.role) and r.branch not in S.scope_branches(user, db):
            continue
        for tw in to_weeks:
            nd = tw + timedelta(days=(r.work_date - from_ws).days)
            _upsert(db, r.employee_id, r.branch, nd,
                    {"start_time": r.start_time, "end_time": r.end_time,
                     "break_minutes": r.break_minutes, "notes": r.notes, "is_off": r.is_off}, user)
            n += 1
    db.commit()
    S.audit(db, user, "schedule_created", "schedule", "copy", detail=f"{from_ws}->{len(to_weeks)}wk {n} shifts")
    return {"copied": n, "weeks": len(to_weeks)}


# ------------------------------------------------------------------ publish/send
def _targets(db, user, employee_id):
    if employee_id:
        e = db.get(models.Employee, employee_id)
        return [e] if e else []
    return _employees_in_scope(db, user)


@router.post("/publish")
def publish(body: dict, db: Session = Depends(get_db),
            user: models.User = Depends(S.require("manage_schedules"))):
    """Mark a week published and send each employee their own schedule."""
    body = body or {}
    ws = SC.week_start_of(body.get("week_start") or str(date.today()))
    targets = _targets(db, user, body.get("employee_id"))
    published, queued, skipped = 0, 0, 0
    stamp = datetime.now(timezone.utc).strftime("%H%M%S%f")
    for emp in targets:
        rows = (db.query(models.EmployeeSchedule)
                .filter(models.EmployeeSchedule.employee_id == emp.id,
                        models.EmployeeSchedule.week_start == ws).all())
        if not rows:
            continue
        for r in rows:
            r.published = True
        db.commit()
        published += 1
        res = _queue(db, emp, ws, "publish", user, unique_suffix=stamp)
        if res and res.status == "queued":
            queued += 1
        elif res and res.status == "skipped":
            skipped += 1
    if body.get("copy_to_me"):
        _copy_to_actor(db, user, ws, targets, stamp)
    S.audit(db, user, "schedule_published", "schedule", (body.get("employee_id") or "all"),
            detail=f"week {ws} · {published} employee(s)")
    return {"week_start": str(ws), "published_employees": published,
            "queued": queued, "skipped": skipped}


@router.post("/send")
def send(body: dict, db: Session = Depends(get_db),
         user: models.User = Depends(S.require("manage_schedules"))):
    """Send schedules now — all employees or one — without changing publish state."""
    body = body or {}
    ws = SC.week_start_of(body.get("week_start") or str(date.today()))
    targets = _targets(db, user, body.get("employee_id"))
    queued, skipped = 0, 0
    stamp = datetime.now(timezone.utc).strftime("%H%M%S%f")
    for emp in targets:
        res = _queue(db, emp, ws, "manual", user, unique_suffix=stamp)
        if res and res.status == "queued":
            queued += 1
        elif res and res.status == "skipped":
            skipped += 1
    if body.get("copy_to_me"):
        _copy_to_actor(db, user, ws, targets, stamp)
    S.audit(db, user, "telegram_schedule_send", "schedule", (body.get("employee_id") or "all"),
            detail=f"week {ws} · queued {queued}")
    return {"week_start": str(ws), "queued": queued, "skipped": skipped}


def _copy_to_actor(db, user, ws, targets, stamp):
    """Optional owner/manager copy: one bundled message of every sent schedule."""
    link = (db.query(models.TelegramLink)
            .filter(models.TelegramLink.user_id == getattr(user, "id", None)).first())
    if not link or (link.status or "active") != "active":
        return
    parts = [f"\U0001F4CB Schedules for week of {ws}", ""]
    for emp in targets:
        parts.append(SC.render_week_message(db, emp, ws))
        parts.append("\n" + ("—" * 12) + "\n")
    key = f"copy|{ws}|{link.tg_id}|{stamp}"
    if db.query(models.TelegramDeliveryLog).filter_by(idem_key=key).first():
        return
    db.add(models.TelegramDeliveryLog(idem_key=key, employee_id=None, tg_id=link.tg_id,
                                      recipient=(user.name if user else "manager"),
                                      week_start=ws, kind="copy", status="queued",
                                      message="\n".join(parts), created_by=getattr(user, "id", None)))
    db.commit()


@router.get("/preview")
def preview(week_start: str = "", employee_id: str = "", db: Session = Depends(get_db),
            user: models.User = Depends(S.require("manage_schedules"))):
    """Exactly what each employee would receive — without sending or logging."""
    ws = SC.week_start_of(week_start or str(date.today()))
    targets = _targets(db, user, employee_id)
    out = []
    for emp in targets:
        out.append({"employee_id": emp.id, "name": emp.name, "branch": emp.branch,
                    "tg_linked": bool(SC.employee_tg(db, emp.id)),
                    "message": SC.render_week_message(db, emp, ws)})
    return {"week_start": str(ws), "previews": out}


# ------------------------------------------------------------------ deliveries
@router.get("/deliveries")
def deliveries(limit: int = 100, db: Session = Depends(get_db),
               user: models.User = Depends(S.require("manage_schedules"))):
    rows = (db.query(models.TelegramDeliveryLog)
            .order_by(models.TelegramDeliveryLog.id.desc()).limit(min(limit, 500)).all())
    return {"deliveries": [{
        "id": r.id, "employee_id": r.employee_id, "recipient": r.recipient,
        "tg_id": r.tg_id, "week_start": str(r.week_start or ""), "kind": r.kind,
        "status": r.status, "error": r.error, "message_id": r.message_id,
        "at": SC.iso(r.created_at)} for r in rows]}


@router.post("/deliveries/{did}/resend")
def resend(did: int, db: Session = Depends(get_db),
           user: models.User = Depends(S.require("manage_schedules"))):
    row = db.get(models.TelegramDeliveryLog, did)
    if not row:
        raise HTTPException(404, "Delivery not found")
    if not row.tg_id:
        raise HTTPException(422, "That recipient has no linked Telegram account.")
    stamp = datetime.now(timezone.utc).strftime("%H%M%S%f")
    key = f"resend|{row.week_start}|{row.tg_id}|{stamp}"
    db.add(models.TelegramDeliveryLog(idem_key=key, employee_id=row.employee_id,
                                      tg_id=row.tg_id, recipient=row.recipient,
                                      week_start=row.week_start, kind="manual",
                                      status="queued", message=row.message,
                                      created_by=getattr(user, "id", None)))
    db.commit()
    S.audit(db, user, "telegram_schedule_resend", "schedule", row.employee_id or "copy",
            detail=str(row.week_start))
    return {"ok": True, "requeued": True}


# ------------------------------------------------------------------ templates
@router.get("/templates")
def templates(db: Session = Depends(get_db),
              user: models.User = Depends(S.require("manage_schedules"))):
    rows = db.query(models.ScheduleTemplate).order_by(models.ScheduleTemplate.id.desc()).all()
    return {"templates": [{"id": t.id, "name": t.name, "recurrence": t.recurrence,
                           "weekdays": json.loads(t.weekdays or "[]"),
                           "every_other": bool(t.every_other), "start_time": t.start_time,
                           "end_time": t.end_time, "break_minutes": t.break_minutes or 0,
                           "notes": t.notes} for t in rows]}


@router.post("/templates", status_code=201)
def create_template(body: dict, db: Session = Depends(get_db),
                    user: models.User = Depends(S.require("manage_schedules"))):
    body = body or {}
    t = models.ScheduleTemplate(
        name=(body.get("name") or "Template").strip(),
        recurrence=body.get("recurrence", "weekly"),
        weekdays=json.dumps([int(x) for x in (body.get("weekdays") or []) if 0 <= int(x) <= 6]),
        every_other=bool(body.get("every_other")),
        start_time=body.get("start_time", "09:00"), end_time=body.get("end_time", "17:00"),
        break_minutes=int(body.get("break_minutes", 0) or 0), notes=body.get("notes"),
        created_by=getattr(user, "id", None))
    db.add(t); db.commit()
    S.audit(db, user, "schedule_template_created", "schedule_template", t.id, detail=t.name)
    return {"id": t.id, "name": t.name}


@router.delete("/templates/{tid}")
def delete_template(tid: int, db: Session = Depends(get_db),
                    user: models.User = Depends(S.require("manage_schedules"))):
    t = db.get(models.ScheduleTemplate, tid)
    if not t:
        raise HTTPException(404, "Not found")
    db.delete(t); db.commit()
    S.audit(db, user, "schedule_template_deleted", "schedule_template", tid)
    return {"ok": True}


# ------------------------------------------------------------------ worker
@router.post("/auto")
def auto_weekly(x_bot_token: str = Header(None), db: Session = Depends(get_db)):
    """Worker tick: the day before a new work week (business-local), enqueue each
    employee's published upcoming-week schedule. Idempotent per week + recipient."""
    _bot(x_bot_token)
    local, tzname = R.now_local(db)
    # tomorrow is a Monday? then 'upcoming week' starts tomorrow.
    if local.weekday() != 6 or local.hour != WEEKLY_SEND_HOUR:
        return {"enqueued": 0, "reason": "not the pre-week slot",
                "local": local.strftime("%Y-%m-%d %H:%M"), "tz": tzname}
    upcoming = SC.week_start_of(local.date() + timedelta(days=1))
    enq = 0
    for emp in (db.query(models.Employee).filter(models.Employee.active == True).all()):  # noqa: E712
        if not SC.has_published_week(db, emp.id, upcoming):
            continue
        res = _queue(db, emp, upcoming, "weekly", None)
        if res and res.status == "queued":
            enq += 1
    return {"enqueued": enq, "week_start": str(upcoming)}


@router.get("/pending")
def pending(x_bot_token: str = Header(None), db: Session = Depends(get_db)):
    """Queued schedule deliveries awaiting the worker."""
    _bot(x_bot_token)
    rows = (db.query(models.TelegramDeliveryLog)
            .filter(models.TelegramDeliveryLog.status == "queued",
                    models.TelegramDeliveryLog.tg_id.isnot(None))
            .order_by(models.TelegramDeliveryLog.id.asc()).limit(50).all())
    return {"pending": [{"idem_key": r.idem_key, "tg_id": r.tg_id, "message": r.message}
                        for r in rows]}


@router.post("/complete")
def complete(body: dict, x_bot_token: str = Header(None), db: Session = Depends(get_db)):
    """Worker reports a delivery outcome — the row is the delivery log."""
    _bot(x_bot_token)
    key = str((body or {}).get("idem_key") or "")
    row = db.query(models.TelegramDeliveryLog).filter_by(idem_key=key).first()
    if not row:
        raise HTTPException(404, "Unknown delivery")
    row.status = str(body.get("status") or "sent")
    row.error = body.get("error")
    row.message_id = str(body.get("message_id") or "")[:60]
    db.commit()
    db.add(models.AuditLog(source="TELEGRAM", tg_id=row.tg_id,
                           action=("telegram_schedule_sent" if row.status == "sent"
                                   else "telegram_schedule_failed"),
                           entity="schedule", ref=row.employee_id or "copy",
                           detail=str(row.week_start or ""),
                           result=("ok" if row.status == "sent" else "denied"), ip="telegram"))
    db.commit()
    return {"ok": True, "status": row.status}
