"""No-Activity Alert — business-hours math, detection, incident lifecycle, RBAC,
Telegram dedup/failure, acknowledgement, auto-resolution, and boundary cases.

Runs on SQLite with the seeded demo company. Deterministic: tests create their own
branches + activity and pass an explicit `now` where timing matters, rather than
depending on the seed's activity timestamps.
"""
import os
import json
import tempfile
from datetime import datetime, timedelta, timezone

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_na_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "noactivity-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import models, noactivity as NA
from app.database import SessionLocal

client = TestClient(app)
PW = "demo1234"
BOT = {"X-Bot-Token": "test-bot-token"}
UTC = timezone.utc


MY_BRANCHES = ["Store LA", "Store REC", "Store ACT", "Store OFF", "Store SCOPED",
               "Store NOACC", "Store ACK", "Store REM", "Store ACKREM", "Store FAIL",
               "Store AUD", "Store TG"]
MY_LINKS = ["tg-owner-1", "tg-acct-1"]


@pytest.fixture(scope="module", autouse=True)
def _boot():
    # The suite shares one process-wide `settings`; other modules mutate bot_token.
    # Pin it so this module's X-Bot-Token worker calls authenticate deterministically.
    from app.config import settings
    settings.bot_token = "test-bot-token"
    with TestClient(app):
        yield
        # The suite shares one process-wide engine/DB; remove everything this module
        # created so later modules (which assert on the full branch/recipient set) are
        # not polluted.
        db = SessionLocal()
        try:
            db.query(models.NoActivityIncident).delete()
            db.query(models.ReminderDelivery).filter(
                models.ReminderDelivery.kind == "noactivity").delete()
            for key in MY_BRANCHES:
                db.query(models.Ledger).filter(models.Ledger.branch == key).delete()
                db.query(models.Movement).filter(models.Movement.branch == key).delete()
                db.query(models.Attendance).filter(models.Attendance.branch == key).delete()
                b = db.get(models.Branch, key)
                if b:
                    db.delete(b)
            for tg in MY_LINKS:
                ln = db.get(models.TelegramLink, tg)
                if ln:
                    db.delete(ln)
            a = db.get(models.Branch, "Store A")   # reset config test's mutation
            if a:
                a.open_time = a.close_time = a.open_days = None
                a.inactivity_alert_enabled = True
                a.inactivity_threshold_hours = 12
            db.commit()
        finally:
            db.close()


def tok(uid):
    r = client.post("/api/auth/login", data={"username": uid, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


# --------------------------------------------------------------- helpers
def _mk_branch(db, key, *, open_t="00:00", close_t="23:59", days=None,
               enabled=True, threshold=12, display=None):
    b = db.get(models.Branch, key)
    if not b:
        b = models.Branch(name=key)
        db.add(b)
    b.display_name = display or key
    b.open_time, b.close_time = open_t, close_t
    b.open_days = json.dumps(days if days is not None else [0, 1, 2, 3, 4, 5, 6])
    b.inactivity_alert_enabled = enabled
    b.inactivity_threshold_hours = threshold
    db.commit()
    return b


def _add_sale(db, branch, when=None, by="U-owner"):
    row = models.Ledger(branch=branch, type="sale", amount=100,
                        created_by=by, entry_date=datetime.now(UTC).date())
    db.add(row)
    db.commit()
    if when is not None:
        row.created_at = when
        db.commit()
    return row


def _link(db, uid, tg_id):
    ln = db.get(models.TelegramLink, tg_id)
    if not ln:
        ln = models.TelegramLink(tg_id=tg_id, user_id=uid, username=f"{uid}_tg",
                                 status="active", linked_at=datetime.now(UTC))
        db.add(ln)
        db.commit()
    return ln


def _incident(db, branch):
    return (db.query(models.NoActivityIncident)
            .filter(models.NoActivityIncident.branch == branch,
                    models.NoActivityIncident.status.in_(NA.ACTIVE_INCIDENT_STATES))
            .order_by(models.NoActivityIncident.id.desc()).first())


# ============================================================ business-hours math
def test_business_hours_same_day_window():
    # 08:00–20:00 window, one full open day → exactly 12 business hours.
    start = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)   # Monday
    end = datetime(2026, 3, 2, 20, 0, tzinfo=UTC)
    h = NA.business_hours_between(start, end, (8, 0), (20, 0), {0, 1, 2, 3, 4}, "UTC")
    assert h == 12.0


def test_business_hours_excludes_closed_days():
    # Fri 18:00 → Mon 10:00, open 08:00–18:00 Mon–Fri. Sat/Sun excluded.
    start = datetime(2026, 3, 6, 18, 0, tzinfo=UTC)  # Friday close
    end = datetime(2026, 3, 9, 10, 0, tzinfo=UTC)    # Monday 10:00
    h = NA.business_hours_between(start, end, (8, 0), (18, 0), {0, 1, 2, 3, 4}, "UTC")
    assert h == 2.0   # only Mon 08:00–10:00 counts; weekend fully excluded


def test_business_hours_overnight_window():
    # Overnight bar hours 20:00–04:00. Mon 22:00 → Tue 02:00 = 4 business hours.
    start = datetime(2026, 3, 2, 22, 0, tzinfo=UTC)
    end = datetime(2026, 3, 3, 2, 0, tzinfo=UTC)
    h = NA.business_hours_between(start, end, (20, 0), (4, 0), {0, 1, 2, 3, 4, 5, 6}, "UTC")
    assert h == 4.0


def test_exactly_threshold_triggers_and_just_under_does_not():
    open_hm, close_hm, days = (8, 0), (20, 0), {0, 1, 2, 3, 4, 5, 6}
    base = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)
    assert NA.business_hours_between(base, base + timedelta(hours=12), open_hm, close_hm, days, "UTC") == 12.0
    assert NA.business_hours_between(base, base + timedelta(hours=11, minutes=59), open_hm, close_hm, days, "UTC") < 12.0


def test_missing_schedule_uses_documented_default():
    db = SessionLocal()
    try:
        b = models.Branch(name="Store NODEF")   # no schedule configured
        _oh, _ch, days, thr, source = NA.branch_schedule(b)
        assert source == "default"
        assert thr == NA.DEFAULT_THRESHOLD_HOURS == 12
        assert days == set(NA.DEFAULT_DAYS)      # Mon–Sat
        assert _oh == (8, 0) and _ch == (22, 0)
    finally:
        db.close()


# ============================================================ activity detection
def test_last_activity_picks_most_recent_across_sources():
    db = SessionLocal()
    try:
        _mk_branch(db, "Store LA")
        old = datetime.now(UTC) - timedelta(days=3)
        recent = datetime.now(UTC) - timedelta(hours=1)
        _add_sale(db, "Store LA", when=old, by="U-cash")
        db.add(models.Movement(branch="Store LA", type="receive", moved_at=recent,
                               user_id="U-inv", qty_change=5))
        db.commit()
        at, typ, by = NA.last_activity(db, "Store LA")
        assert typ == "movement" and by == "U-inv"
        assert abs((NA._aware(at) - recent).total_seconds()) < 2
    finally:
        db.close()


# ============================================================ incident lifecycle
def test_reconcile_opens_then_autoresolves():
    db = SessionLocal()
    try:
        _mk_branch(db, "Store REC")            # 24/7 open, no activity → inactive
        now = datetime.now(UTC)
        active = NA.reconcile(db, [db.get(models.Branch, "Store REC")], now, "UTC")
        assert any(i.branch == "Store REC" for i in active)
        inc = _incident(db, "Store REC")
        assert inc and inc.status == "open" and inc.business_hours_idle >= 12
        # New activity → auto-resolve
        _add_sale(db, "Store REC", when=now)
        NA.reconcile(db, [db.get(models.Branch, "Store REC")], now, "UTC")
        assert _incident(db, "Store REC") is None
        resolved = (db.query(models.NoActivityIncident)
                    .filter(models.NoActivityIncident.branch == "Store REC",
                            models.NoActivityIncident.status == "resolved").first())
        assert resolved is not None and resolved.resolved_at is not None
    finally:
        db.close()


def test_active_branch_never_opens_incident():
    db = SessionLocal()
    try:
        _mk_branch(db, "Store ACT")
        _add_sale(db, "Store ACT", when=datetime.now(UTC))   # fresh activity
        NA.reconcile(db, [db.get(models.Branch, "Store ACT")], datetime.now(UTC), "UTC")
        assert _incident(db, "Store ACT") is None
    finally:
        db.close()


def test_disabled_branch_no_incident():
    db = SessionLocal()
    try:
        _mk_branch(db, "Store OFF", enabled=False)   # inactive but alerts disabled
        NA.reconcile(db, [db.get(models.Branch, "Store OFF")], datetime.now(UTC), "UTC")
        assert _incident(db, "Store OFF") is None
    finally:
        db.close()


# ============================================================ RBAC / scoping
def test_dashboard_alerts_scoped_to_authorized_branches():
    db = SessionLocal()
    try:
        _mk_branch(db, "Store SCOPED")
        NA.reconcile(db, [db.get(models.Branch, "Store SCOPED")], datetime.now(UTC), "UTC")
    finally:
        db.close()
    # Owner sees the incident; cashier (scoped to Store A only) does not.
    owner = client.get("/api/reports/no-activity-alerts", headers=tok("U-owner")).json()
    assert any(i["branch"] == "Store SCOPED" for i in owner["items"])
    cash = client.get("/api/reports/no-activity-alerts", headers=tok("U-cash")).json()
    assert all(i["branch"] != "Store SCOPED" for i in cash["items"])


def test_acknowledge_inaccessible_branch_forbidden():
    db = SessionLocal()
    try:
        _mk_branch(db, "Store NOACC")
        NA.reconcile(db, [db.get(models.Branch, "Store NOACC")], datetime.now(UTC), "UTC")
    finally:
        db.close()
    r = client.post("/api/reports/no-activity-alerts/Store NOACC/acknowledge", headers=tok("U-cash"))
    assert r.status_code == 403


# ============================================================ acknowledgement
def test_acknowledge_stops_reminders_but_stays_visible_unresolved():
    db = SessionLocal()
    try:
        _mk_branch(db, "Store ACK")
        NA.reconcile(db, [db.get(models.Branch, "Store ACK")], datetime.now(UTC), "UTC")
    finally:
        db.close()
    r = client.post("/api/reports/no-activity-alerts/Store ACK/acknowledge", headers=tok("U-owner"))
    assert r.status_code == 200 and r.json()["status"] == "acknowledged"
    # Still shown on the dashboard (must not be falsely resolved)
    feed = client.get("/api/reports/no-activity-alerts", headers=tok("U-owner")).json()
    item = next(i for i in feed["items"] if i["branch"] == "Store ACK")
    assert item["acknowledged"] is True
    db = SessionLocal()
    try:
        inc = _incident(db, "Store ACK")
        assert inc.status == "acknowledged" and inc.resolved_at is None
    finally:
        db.close()


# ============================================================ Telegram dedup / reminders / failure
def test_scan_sends_one_initial_then_dedups():
    db = SessionLocal()
    try:
        _mk_branch(db, "Store TG")
        _link(db, "U-owner", "tg-owner-1")
        _link(db, "U-acct", "tg-acct-1")
    finally:
        db.close()
    first = client.post("/api/telegram/noactivity/scan", headers=BOT).json()
    q1 = [x for x in first["queued"] if "|initial|" in x["idem_key"] and "Store TG" in x["message"]]
    tgids = {x["tg_id"] for x in q1}
    assert "tg-owner-1" in tgids and "tg-acct-1" in tgids   # both owner + accountant notified
    # Second scan must NOT re-queue the initial notification (idempotent).
    second = client.post("/api/telegram/noactivity/scan", headers=BOT).json()
    assert not [x for x in second["queued"]
                if "Store TG" in x.get("message", "") and "|initial|" in x["idem_key"]]


def test_scan_daily_reminder_after_24h_only():
    db = SessionLocal()
    try:
        _mk_branch(db, "Store REM")
        _link(db, "U-owner", "tg-owner-1")
    finally:
        db.close()
    client.post("/api/telegram/noactivity/scan", headers=BOT)   # initial
    db = SessionLocal()
    try:
        inc = _incident(db, "Store REM")
        # <24h since notification → no reminder
        inc.notified_at = datetime.now(UTC) - timedelta(hours=1)
        inc.last_reminder_at = None
        db.commit()
    finally:
        db.close()
    r = client.post("/api/telegram/noactivity/scan", headers=BOT).json()
    assert not [x for x in r["queued"] if "Store REM" in x.get("message", "") and "|reminder|" in x["idem_key"]]
    db = SessionLocal()
    try:
        inc = _incident(db, "Store REM")
        inc.notified_at = datetime.now(UTC) - timedelta(hours=30)  # >24h
        inc.last_reminder_at = None
        db.commit()
    finally:
        db.close()
    r = client.post("/api/telegram/noactivity/scan", headers=BOT).json()
    assert [x for x in r["queued"] if "Store REM" in x.get("message", "") and "|reminder|" in x["idem_key"]]


def test_acknowledged_incident_gets_no_reminders():
    db = SessionLocal()
    try:
        _mk_branch(db, "Store ACKREM")
        _link(db, "U-owner", "tg-owner-1")
    finally:
        db.close()
    client.post("/api/telegram/noactivity/scan", headers=BOT)          # initial
    client.post("/api/reports/no-activity-alerts/Store ACKREM/acknowledge", headers=tok("U-owner"))
    db = SessionLocal()
    try:
        inc = _incident(db, "Store ACKREM")
        inc.notified_at = datetime.now(UTC) - timedelta(hours=48)
        db.commit()
    finally:
        db.close()
    r = client.post("/api/telegram/noactivity/scan", headers=BOT).json()
    assert not [x for x in r["queued"] if "Store ACKREM" in x.get("message", "")]


def test_telegram_delivery_failure_marks_failed():
    db = SessionLocal()
    try:
        _mk_branch(db, "Store FAIL")
        _link(db, "U-owner", "tg-owner-1")
    finally:
        db.close()
    client.post("/api/telegram/noactivity/scan", headers=BOT)
    db = SessionLocal()
    try:
        inc = _incident(db, "Store FAIL")
        key = f"noactivity|{inc.id}|initial|tg-owner-1"
        assert db.query(models.ReminderDelivery).filter(
            models.ReminderDelivery.idem_key == key).first() is not None
    finally:
        db.close()
    # Worker reports a send failure via the shared completion endpoint.
    r = client.post("/api/telegram/reminders/complete", headers=BOT,
                    json={"idem_key": key, "status": "failed", "error": "chat not found"})
    assert r.status_code == 200 and r.json()["status"] == "failed"
    db = SessionLocal()
    try:
        row = db.query(models.ReminderDelivery).filter(models.ReminderDelivery.idem_key == key).first()
        assert row.status == "failed" and row.kind == "noactivity"
    finally:
        db.close()


def test_scan_requires_bot_token():
    assert client.post("/api/telegram/noactivity/scan").status_code == 403
    assert client.post("/api/telegram/noactivity/scan", headers={"X-Bot-Token": "wrong"}).status_code == 403


# ============================================================ config + audit
def test_config_get_and_put_roundtrip():
    h = tok("U-owner")
    got = client.get("/api/branches/Store A/inactivity-config", headers=h).json()
    assert got["schedule_source"] in ("configured", "default")
    put = client.put("/api/branches/Store A/inactivity-config", headers=h,
                     json={"threshold_hours": 8, "open_time": "09:00", "close_time": "21:00",
                           "open_days": [0, 1, 2, 3, 4], "alert_enabled": True}).json()
    assert put["threshold_hours"] == 8 and put["schedule_source"] == "configured"
    assert put["open_days"] == [0, 1, 2, 3, 4]


def test_config_put_requires_manage_branches():
    # cashier lacks manage_branches
    r = client.put("/api/branches/Store A/inactivity-config", headers=tok("U-cash"),
                   json={"threshold_hours": 6})
    assert r.status_code == 403


def test_audit_records_open_and_resolution():
    db = SessionLocal()
    try:
        _mk_branch(db, "Store AUD")
        now = datetime.now(UTC)
        NA.reconcile(db, [db.get(models.Branch, "Store AUD")], now, "UTC")
        _add_sale(db, "Store AUD", when=now)
        NA.reconcile(db, [db.get(models.Branch, "Store AUD")], now, "UTC")
        actions = {a.action for a in db.query(models.AuditLog)
                   .filter(models.AuditLog.ref == "Store AUD").all()}
        assert "no_activity_incident_opened" in actions
        assert "no_activity_incident_resolved" in actions
    finally:
        db.close()
