"""Recurring Telegram reminders — schedule + recipient logic.

Design goals (mirrors the scheduled-reports module deliberately):

* **No AI, no external scheduler.** Everything is deterministic Python inside
  the existing backend. The Telegram worker's 60-second loop is the only clock.
* **Restart-safe.** The whole schedule is one database row (`reminder_settings`)
  plus the company timezone. `next_run_at` is the source of truth; the worker
  holds nothing in memory, so a restart, redeploy or a second instance loses
  nothing and never double-sends (idempotency ledger on `reminder_deliveries`).
* **Timezone-correct.** Slots are computed in the *business* timezone via
  `reports_tg.now_local`, so "every 12h between 08:00–22:00" means 08:00 and
  20:00 local, and stays correct across a daylight-saving change.
"""
import json
from datetime import datetime, timedelta, timezone

from . import models
from . import reports_tg as R

# The exact default reminder, editable per-company in Admin Settings.
DEFAULT_MESSAGE = (
    "\U0001F514 Friendly Reminder\n"
    "Please make sure all business data has been entered into SmokeStack ERP.\n"
    "Before finishing your shift, please verify that the following have been "
    "recorded if applicable:\n"
    "• Sales\n"
    "• Purchases\n"
    "• Expenses\n"
    "• Inventory movements\n"
    "• Employee attendance\n"
    "• Cash transactions\n"
    "• Customer updates\n"
    "Keeping the system up to date ensures accurate reports and business insights.\n"
    "Thank you."
)

DEFAULTS = dict(enabled=False, interval_hours=12, active_start_hour=8,
                active_end_hour=22, recipient_mode="all")

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]   # 0..6, matches datetime.weekday()


# --------------------------------------------------------------------- settings
def get_settings(db):
    """The single settings row, created with sane defaults on first access."""
    s = db.get(models.ReminderSetting, 1)
    if not s:
        s = models.ReminderSetting(id=1, message=DEFAULT_MESSAGE, **DEFAULTS)
        db.add(s)
        db.commit()
        db.refresh(s)
    if not s.message:
        s.message = DEFAULT_MESSAGE
    return s


def paused_days(s):
    try:
        return set(int(x) for x in json.loads(s.paused_days or "[]"))
    except Exception:  # noqa: BLE001
        return set()


def recipient_selection(s):
    if (s.recipient_mode or "all") != "selected":
        return None
    try:
        return set(str(x) for x in json.loads(s.recipient_ids or "[]"))
    except Exception:  # noqa: BLE001
        return set()


# ------------------------------------------------------------------ scheduling
def daily_slot_hours(s):
    """The local hours a reminder fires at: anchored at active_start_hour and
    stepped by the interval, never past active_end_hour.

    interval 12h, window 08:00–22:00  -> [8, 20]
    interval  6h, window 08:00–22:00  -> [8, 14, 20]
    interval 24h, window 08:00–22:00  -> [8]
    """
    step = max(1, int(s.interval_hours or 12))
    start = max(0, min(23, int(s.active_start_hour if s.active_start_hour is not None else 8)))
    end = max(0, min(23, int(s.active_end_hour if s.active_end_hour is not None else 22)))
    if end < start:
        end = start
    hours, h = [], start
    while h <= end:
        hours.append(h)
        h += step
    return hours or [start]


def is_active_time(local_dt, s):
    """Is this local datetime inside the active window and not on a paused day?"""
    if local_dt.weekday() in paused_days(s):
        return False
    start = int(s.active_start_hour if s.active_start_hour is not None else 8)
    end = int(s.active_end_hour if s.active_end_hour is not None else 22)
    return start <= local_dt.hour <= end


def compute_next_run(local_now, s):
    """Next fire time strictly after local_now, honouring interval, active window
    and paused days. Returned as an aware UTC datetime."""
    hours = daily_slot_hours(s)
    paused = paused_days(s)
    for d in range(0, 15):                       # search up to two weeks out
        base = (local_now + timedelta(days=d)).replace(minute=0, second=0, microsecond=0)
        if base.weekday() in paused:
            continue
        for h in hours:
            cand = base.replace(hour=h)
            if cand > local_now:
                return cand.astimezone(timezone.utc)
    # Fallback (e.g. every day paused): just add the interval so we never wedge.
    return (local_now + timedelta(hours=max(1, int(s.interval_hours or 12)))).astimezone(timezone.utc)


def next_run_local(db, s):
    """The stored next_run_at rendered in the business timezone (for the UI)."""
    if not s.next_run_at:
        return None
    tzname = R.company_tz(db)
    try:
        from zoneinfo import ZoneInfo
        nr = s.next_run_at
        if nr.tzinfo is None:
            nr = nr.replace(tzinfo=timezone.utc)
        return nr.astimezone(ZoneInfo(tzname))
    except Exception:  # noqa: BLE001
        return s.next_run_at


def ensure_next_run(db, s):
    """Make sure next_run_at is populated (first enable, or after a config change)."""
    local, _ = R.now_local(db)
    s.next_run_at = compute_next_run(local, s)
    db.commit()
    return s.next_run_at


# ------------------------------------------------------------------ recipients
def resolve_recipients(db, s):
    """Linked, active Telegram accounts that should receive the reminder.

    Recipients are the same linked accounts the rest of the integration uses, so
    a reminder can only ever reach an account an administrator has already
    linked and left enabled. `selected` mode narrows to a chosen subset.
    """
    sel = recipient_selection(s)
    out = []
    for link in db.query(models.TelegramLink).order_by(models.TelegramLink.linked_at.asc()).all():
        if (link.status or "active") != "active":
            continue
        if sel is not None and link.tg_id not in sel:
            continue
        u = db.get(models.User, link.user_id)
        name = (u.name if u else None) or link.username or link.tg_id
        out.append({"tg_id": link.tg_id, "name": name,
                    "username": link.username, "role": (u.role if u else None)})
    return out


def candidate_accounts(db):
    """Every linked account, for the recipient picker in the settings UI."""
    rows = []
    for link in db.query(models.TelegramLink).order_by(models.TelegramLink.linked_at.asc()).all():
        u = db.get(models.User, link.user_id)
        rows.append({"tg_id": link.tg_id, "username": link.username,
                     "name": (u.name if u else None) or link.username or link.tg_id,
                     "role": (u.role if u else None),
                     "status": (link.status or "active")})
    return rows


def iso(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()
