"""Employee Work Schedule — week math, recurrence expansion, message rendering
and Telegram-recipient resolution.

No AI, no external calendar. Weeks are ISO weeks (Monday start). Recurrence is
expanded deterministically into concrete dated shifts, so the calendar, the
published week and the Telegram message all read from the same rows.
"""
import json
from datetime import date, datetime, timedelta, timezone

from . import models
from . import reports_tg as R

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday"]           # index == weekday()

DEFAULT_MESSAGE_NOTE = "Please contact your manager if you have any questions."


# --------------------------------------------------------------------- weeks
def week_start_of(d):
    """Monday of the ISO week containing date d."""
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return d - timedelta(days=d.weekday())


def week_dates(week_start):
    if isinstance(week_start, str):
        week_start = date.fromisoformat(week_start)
    return [week_start + timedelta(days=i) for i in range(7)]


# ---------------------------------------------------------------- recurrence
def _weekdays_for(recurrence, weekdays):
    """Resolve a recurrence name (+ explicit custom list) to weekday ints 0..6."""
    if recurrence == "weekdays":
        return [0, 1, 2, 3, 4]
    if recurrence == "weekends":
        return [5, 6]
    # weekly / alternate / custom all use the explicit list
    out = []
    for x in (weekdays or []):
        try:
            v = int(x)
            if 0 <= v <= 6:
                out.append(v)
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def expand_dates(start, weeks, recurrence, weekdays, every_other=False):
    """Concrete dates a pattern covers, starting at `start`'s week for `weeks`
    weeks. `every_other` (alternate) includes only even-offset weeks."""
    if isinstance(start, str):
        start = date.fromisoformat(start)
    base = week_start_of(start)
    wd = _weekdays_for(recurrence, weekdays)
    alt = every_other or recurrence == "alternate"
    out = []
    for w in range(max(1, int(weeks or 1))):
        if alt and (w % 2 == 1):
            continue
        wk = base + timedelta(days=7 * w)
        for i in wd:
            out.append(wk + timedelta(days=i))
    return out


# ------------------------------------------------------------------ employees
def employee_tg(db, employee_id):
    """The active linked Telegram account for an employee, or None."""
    link = (db.query(models.TelegramLink)
            .filter(models.TelegramLink.employee_id == employee_id).first())
    if not link:
        # fall back to matching via the employee's provisioned user_id
        emp = db.get(models.Employee, employee_id)
        if emp and emp.user_id:
            link = (db.query(models.TelegramLink)
                    .filter(models.TelegramLink.user_id == emp.user_id).first())
    if link and (link.status or "active") == "active":
        return link
    return None


def employee_of_user(db, user):
    """Resolve the Employee a logged-in user represents (for read-only /my)."""
    uid = getattr(user, "id", None)
    emp = db.query(models.Employee).filter(models.Employee.user_id == uid).first()
    if not emp:
        emp = db.query(models.Employee).filter(models.Employee.name == getattr(user, "name", None)).first()
    return emp


# ------------------------------------------------------------------ rendering
def _fmt_day(row):
    if not row or row.get("is_off"):
        return "OFF"
    s = row.get("start_time") or "09:00"
    e = row.get("end_time") or "17:00"
    line = f"{s} - {e}"
    br = row.get("break_minutes") or 0
    if br:
        line += f"  (break {br}m)"
    return line


def week_map(db, employee_id, week_start):
    """weekday(0..6) -> shift dict for an employee's week, exceptions applied."""
    dates = week_dates(week_start)
    rows = (db.query(models.EmployeeSchedule)
            .filter(models.EmployeeSchedule.employee_id == employee_id,
                    models.EmployeeSchedule.work_date >= dates[0],
                    models.EmployeeSchedule.work_date <= dates[6]).all())
    by_date = {r.work_date: r for r in rows}
    excs = (db.query(models.ScheduleException)
            .filter(models.ScheduleException.employee_id == employee_id,
                    models.ScheduleException.work_date >= dates[0],
                    models.ScheduleException.work_date <= dates[6]).all())
    exc_by_date = {e.work_date: e for e in excs}
    out = {}
    for i, d in enumerate(dates):
        ex = exc_by_date.get(d)
        if ex is not None:
            if ex.kind == "off":
                out[i] = {"is_off": True}
            else:
                out[i] = {"is_off": False, "start_time": ex.start_time,
                          "end_time": ex.end_time, "break_minutes": ex.break_minutes}
            continue
        r = by_date.get(d)
        if r is None:
            out[i] = None
        else:
            out[i] = {"is_off": bool(r.is_off), "start_time": r.start_time,
                      "end_time": r.end_time, "break_minutes": r.break_minutes,
                      "notes": r.notes}
    return out


def render_week_message(db, employee, week_start):
    """The exact Telegram schedule message for one employee's week."""
    name = employee.name if employee else "there"
    wm = week_map(db, employee.id, week_start)
    lines = ["\U0001F4C5 Your Work Schedule", f"Hello {name},",
             "Here is your upcoming work schedule.", ""]
    any_notes = []
    for i in range(7):
        lines.append(DAY_NAMES[i])
        lines.append(_fmt_day(wm.get(i)))
        row = wm.get(i)
        if row and row.get("notes"):
            any_notes.append(f"{DAY_NAMES[i]}: {row['notes']}")
    lines.append("")
    if any_notes:
        lines.append("Notes:")
        lines.extend("• " + n for n in any_notes)
        lines.append("")
    lines.append(DEFAULT_MESSAGE_NOTE)
    return "\n".join(lines)


def has_published_week(db, employee_id, week_start):
    dates = week_dates(week_start)
    return (db.query(models.EmployeeSchedule)
            .filter(models.EmployeeSchedule.employee_id == employee_id,
                    models.EmployeeSchedule.week_start == week_start,
                    models.EmployeeSchedule.published == True)  # noqa: E712
            .count()) > 0


def iso(dt):
    if not dt:
        return None
    if isinstance(dt, datetime) and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
