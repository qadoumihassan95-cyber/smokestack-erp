"""No-Activity Alert — detection, business-hours math and incident lifecycle.

Pure, reusable service used by BOTH the dashboard read endpoint (read-only view of
current incidents) and the Telegram worker `scan` (which also opens/resolves incidents
and queues notifications). All money/activity queries reuse the existing tables; all
Telegram idempotency reuses the `reminder_deliveries` ledger. Nothing here duplicates
the reminder/report scheduling infrastructure.

Meaningful activity = Ledger(sale|expense|purchase|payroll) + Movement(any) +
Attendance(any). Logins, page views, searches and settings changes are never counted.

Business hours are computed per branch in its own timezone, counting only open hours on
open days. When a branch has no configured schedule a DOCUMENTED SAFE DEFAULT is used and
surfaced as schedule_source="default".
"""
import json
from datetime import datetime, timedelta, time, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

from . import models
from .reports_tg import company_tz

# ---- Documented safe defaults (used when a branch has no configured schedule) ----
DEFAULT_OPEN = "08:00"
DEFAULT_CLOSE = "22:00"
DEFAULT_DAYS = [0, 1, 2, 3, 4, 5]      # Mon–Sat open, Sunday closed
DEFAULT_THRESHOLD_HOURS = 12
# When a branch has never recorded ANY activity we measure inactivity over this
# trailing window (so a brand-new/empty branch still alerts once it has been open
# long enough) rather than reaching back to the beginning of time.
NO_HISTORY_LOOKBACK_DAYS = 14
REMINDER_INTERVAL_HOURS = 24            # optional daily reminder while inactivity continues

MEANINGFUL_LEDGER_TYPES = ["sale", "expense", "purchase", "payroll"]
ACTIVE_INCIDENT_STATES = ["open", "acknowledged"]


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hhmm(value, default):
    try:
        h, m = str(value).split(":")
        return int(h), int(m)
    except Exception:  # noqa: BLE001
        h, m = default.split(":")
        return int(h), int(m)


def branch_schedule(b):
    """(open (h,m), close (h,m), open_days set, threshold, source) for a Branch row.

    source is "configured" only when open_time, close_time AND open_days are all set;
    otherwise the documented safe default is used and source is "default"."""
    configured = bool(b.open_time and b.close_time and b.open_days)
    open_hm = _hhmm(b.open_time, DEFAULT_OPEN)
    close_hm = _hhmm(b.close_time, DEFAULT_CLOSE)
    days = DEFAULT_DAYS
    if b.open_days:
        try:
            parsed = json.loads(b.open_days)
            if isinstance(parsed, list) and parsed:
                days = [int(d) for d in parsed]
        except Exception:  # noqa: BLE001
            days = DEFAULT_DAYS
    threshold = int(b.inactivity_threshold_hours or DEFAULT_THRESHOLD_HOURS)
    return open_hm, close_hm, set(days), threshold, ("configured" if configured else "default")


def business_hours_between(start_utc, end_utc, open_hm, close_hm, open_days, tzname):
    """Business hours between two UTC instants: only open-window time on open days.

    Handles overnight windows (close <= open means the window spans midnight),
    weekends/closed days (excluded via open_days) and partial first/last days."""
    start_utc = _aware(start_utc)
    end_utc = _aware(end_utc)
    if start_utc is None or end_utc is None or start_utc >= end_utc:
        return 0.0
    tz = ZoneInfo(tzname) if ZoneInfo else timezone.utc
    start = start_utc.astimezone(tz)
    end = end_utc.astimezone(tz)
    oh, om = open_hm
    ch, cm = close_hm
    overnight = (ch * 60 + cm) <= (oh * 60 + om)
    total = 0.0
    day = start.date()
    last = end.date()
    while day <= last:
        if day.weekday() in open_days:
            win_start = datetime.combine(day, time(oh, om), tz)
            end_day = day + timedelta(days=1) if overnight else day
            win_end = datetime.combine(end_day, time(ch, cm), tz)
            lo = max(win_start, start)
            hi = min(win_end, end)
            if hi > lo:
                total += (hi - lo).total_seconds() / 3600.0
        day += timedelta(days=1)
    return round(total, 3)


def last_activity(db, branch):
    """(utc_datetime, type, recorded_by) of the most recent MEANINGFUL activity for a
    branch, or (None, None, None) if it has never had any. Reuses existing tables."""
    cands = []
    lg = (db.query(models.Ledger.created_at, models.Ledger.type, models.Ledger.created_by)
          .filter(models.Ledger.branch == branch,
                  models.Ledger.type.in_(MEANINGFUL_LEDGER_TYPES))
          .order_by(models.Ledger.created_at.desc()).first())
    if lg and lg[0]:
        cands.append((_aware(lg[0]), lg[1], lg[2]))
    mv = (db.query(models.Movement.moved_at, models.Movement.type, models.Movement.user_id)
          .filter(models.Movement.branch == branch)
          .order_by(models.Movement.moved_at.desc()).first())
    if mv and mv[0]:
        cands.append((_aware(mv[0]), "movement", mv[2]))
    at = (db.query(models.Attendance.clock_in_at, models.Attendance.created_at,
                   models.Attendance.employee_name, models.Attendance.user_id)
          .filter(models.Attendance.branch == branch)
          .order_by(models.Attendance.created_at.desc()).first())
    if at:
        ts = at[0] or at[1]
        if ts:
            cands.append((_aware(ts), "attendance", at[2] or at[3]))
    if not cands:
        return (None, None, None)
    cands.sort(key=lambda x: x[0], reverse=True)
    return cands[0]


def evaluate_branch(db, b, now_utc, tzname):
    """Compute the current inactivity state for one Branch row (no writes).

    Returns a dict describing whether the branch is inactive and the supporting
    figures, always using branch_schedule's documented default when unconfigured."""
    open_hm, close_hm, days, threshold, source = branch_schedule(b)
    last_at, last_type, last_by = last_activity(db, b.name)
    ref = last_at or (now_utc - timedelta(days=NO_HISTORY_LOOKBACK_DAYS))
    idle = business_hours_between(ref, now_utc, open_hm, close_hm, days, tzname)
    enabled = bool(b.inactivity_alert_enabled if b.inactivity_alert_enabled is not None else True)
    inactive = enabled and idle >= threshold
    return {
        "branch": b.name,
        "enabled": enabled,
        "inactive": inactive,
        "business_hours_idle": idle,
        "threshold_hours": threshold,
        "schedule_source": source,
        "last_activity_at": last_at,
        "last_activity_type": last_type,
        "last_activity_by": last_by,
    }


def _active_incident(db, branch):
    return (db.query(models.NoActivityIncident)
            .filter(models.NoActivityIncident.branch == branch,
                    models.NoActivityIncident.status.in_(ACTIVE_INCIDENT_STATES))
            .order_by(models.NoActivityIncident.id.desc()).first())


def reconcile(db, branches, now_utc, tzname):
    """Open incidents for newly-inactive branches and auto-resolve ones that regained
    activity. Idempotent: at most one active incident per branch. Returns the list of
    currently active (open|acknowledged) incidents after reconciliation.

    Does NOT queue Telegram — that is the worker's job (see scan)."""
    active = []
    for b in branches:
        ev = evaluate_branch(db, b, now_utc, tzname)
        inc = _active_incident(db, b.name)
        if ev["inactive"]:
            if inc is None:
                inc = models.NoActivityIncident(
                    branch=b.name, status="open",
                    threshold_hours=ev["threshold_hours"],
                    business_hours_idle=ev["business_hours_idle"],
                    schedule_source=ev["schedule_source"],
                    last_activity_at=ev["last_activity_at"],
                    last_activity_type=ev["last_activity_type"],
                    last_activity_by=ev["last_activity_by"],
                    opened_at=now_utc)
                # SEC HIGH-10: the scan is bot-driven and iterates EVERY company's
                # branches on a session with no company context, so _stamp_writes has
                # nothing to apply and each incident would take the company_id server
                # default of 1 — including incidents opened against another tenant's
                # branch. The incident belongs to whoever owns the branch it is about.
                inc.company_id = getattr(b, "company_id", None) or 1
                db.add(inc)
                db.flush()
                db.add(models.AuditLog(
                    source="SYSTEM", action="no_activity_incident_opened",
                    entity="branch", ref=b.name,
                    detail=f"idle={ev['business_hours_idle']}h threshold={ev['threshold_hours']}h "
                           f"last={ev['last_activity_type'] or 'none'} source={ev['schedule_source']}",
                    result="ok"))
            else:
                inc.business_hours_idle = ev["business_hours_idle"]
                inc.last_activity_at = ev["last_activity_at"]
                inc.last_activity_type = ev["last_activity_type"]
                inc.last_activity_by = ev["last_activity_by"]
            active.append(inc)
        elif inc is not None:
            inc.status = "resolved"
            inc.resolved_at = now_utc
            inc.resolved_activity_type = ev["last_activity_type"]
            db.add(models.AuditLog(
                source="SYSTEM", action="no_activity_incident_resolved",
                entity="branch", ref=b.name,
                detail=f"resolved by {ev['last_activity_type'] or 'activity'}", result="ok"))
    db.commit()
    return active


def resolve_alert_recipients(db):
    """Active linked Telegram accounts of the authorized OWNER and ACCOUNTANT.

    Owners and accountants are all-branch roles, so they are authorized for every
    branch's alerts. Reuses TelegramLink (the same recipient source every other
    Telegram feature uses) — an alert can only reach an already-linked, enabled
    account."""
    out = []
    for link in (db.query(models.TelegramLink)
                 .order_by(models.TelegramLink.linked_at.asc()).all()):
        if (link.status or "active") != "active":
            continue
        u = db.get(models.User, link.user_id)
        if not u or u.role not in ("owner", "accountant"):
            continue
        out.append({"tg_id": link.tg_id,
                    "name": (u.name or link.username or link.tg_id),
                    "role": u.role})
    return out


def render_message(branch_display, ev_or_inc, app_url, reminder=False):
    """The user-facing Telegram body. Uses the branch DISPLAY name; keeps the timestamp
    in the business timezone-agnostic ISO form the rest of the bot uses."""
    idle = ev_or_inc.get("business_hours_idle")
    threshold = ev_or_inc.get("threshold_hours")
    last_at = ev_or_inc.get("last_activity_at")
    last_type = ev_or_inc.get("last_activity_type")
    last_by = ev_or_inc.get("last_activity_by")
    head = "🔴 No Activity Alert" + (" (reminder)" if reminder else "")
    lines = [
        f"{head} — {branch_display}",
        f"No new information has been added for {branch_display} during the last "
        f"{idle:g} business hours (threshold {threshold}h).",
    ]
    if last_at:
        when = _aware(last_at).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        who = f" by {last_by}" if last_by else ""
        lines.append(f"Last activity: {last_type}{who} on {when}.")
    else:
        lines.append("Last activity: none recorded.")
    lines.append(f"Open SmokeStack: {app_url}")
    return "\n".join(lines)


def public_view(db, incidents, now_utc, tzname):
    """Serialize active incidents for the dashboard banner, refreshing the live idle
    figure so the banner is accurate between worker cycles. `incidents` must already be
    scoped to what the caller may see."""
    labels = {b.name: (b.display_name or b.name) for b in db.query(models.Branch).all()}
    schedules = {b.name: b for b in db.query(models.Branch).all()}
    items = []
    for inc in incidents:
        b = schedules.get(inc.branch)
        if b is not None:
            open_hm, close_hm, days, threshold, source = branch_schedule(b)
            ref = _aware(inc.last_activity_at) or (now_utc - timedelta(days=NO_HISTORY_LOOKBACK_DAYS))
            idle = business_hours_between(ref, now_utc, open_hm, close_hm, days, tzname)
        else:
            idle = float(inc.business_hours_idle or 0)
        items.append({
            "branch": inc.branch,
            "branch_display": labels.get(inc.branch, inc.branch),
            "status": inc.status,
            "business_hours_idle": round(idle, 1),
            "threshold_hours": inc.threshold_hours,
            "schedule_source": inc.schedule_source,
            "last_activity_at": _aware(inc.last_activity_at).isoformat() if inc.last_activity_at else None,
            "last_activity_type": inc.last_activity_type,
            "last_activity_by": inc.last_activity_by,
            "opened_at": _aware(inc.opened_at).isoformat() if inc.opened_at else None,
            "acknowledged": inc.status == "acknowledged",
            "acknowledged_by": inc.acknowledged_by,
        })
    return items
