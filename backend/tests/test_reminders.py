"""Recurring Telegram reminders — scheduler, interval, enable/disable, timezone,
active-hours/paused-day suppression, restart persistence, delivery logging, RBAC.

No AI, no external scheduler: the schedule is a database row + company timezone,
so these tests drive the exact same code paths the worker's 60s loop calls.
"""
import os, tempfile
from datetime import datetime, timezone, timedelta

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_rem_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "reminders-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

from fastapi.testclient import TestClient
from app.main import app
from app import reminders_tg as RM
from app.config import settings
from app.database import SessionLocal
from app import models

client = TestClient(app)


def _bot():
    if not settings.bot_token:
        settings.bot_token = "test-bot-token"
    return {"X-Bot-Token": settings.bot_token}


def _tok(uid="U-owner"):
    r = client.post("/api/auth/login", data={"username": uid, "password": "demo1234"})
    return {"Authorization": "Bearer " + r.json()["access_token"]}


CAST = [("RM-OWNER", "Rem Owner", "Store A", "owner", "82001"),
        ("RM-MGR", "Rem Manager", "Store B", "branch_manager", "82002")]


def _setup():
    h = _tok()
    for eid, name, branch, role, tg in CAST:
        client.post("/api/employees", headers=h, json={"id": eid, "name": name, "branch": branch,
                    "title": "Staff", "pay_type": "salary", "salary": 1000, "role": role})
        c = client.post("/api/telegram/link-code", headers=h,
                        json={"employee_id": eid}).json().get("code")
        if c:
            client.post("/api/telegram/link/verify",
                        json={"tg_id": tg, "code": c, "username": "rem_" + eid.lower()})


def _force_due(minutes_ago=1):
    """Push next_run_at into the past so the very next claim fires immediately."""
    db = SessionLocal()
    try:
        s = db.get(models.ReminderSetting, 1)
        s.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------- defaults
def test_default_settings_and_message():
    with TestClient(app):
        _setup()
        r = client.get("/api/telegram/reminders/settings", headers=_tok()).json()
        assert r["enabled"] is False
        assert r["interval_hours"] == 12
        assert r["active_start_hour"] == 8 and r["active_end_hour"] == 22
        # the exact default reminder text, verbatim
        assert r["message"] == RM.DEFAULT_MESSAGE
        assert "Friendly Reminder" in r["message"]
        for bullet in ("Sales", "Purchases", "Expenses", "Inventory movements",
                       "Employee attendance", "Cash transactions", "Customer updates"):
            assert bullet in r["message"]
        assert r["timezone"]                       # a business timezone is reported


# ------------------------------------------------------------------------- RBAC
def test_rbac_only_privileged_roles_can_read_or_change():
    with TestClient(app):
        # a low-privilege account
        h = _tok()
        client.post("/api/employees", headers=h, json={"id": "RM-CASH", "name": "Cash Rem",
                    "branch": "Store A", "title": "Cashier", "pay_type": "hourly",
                    "wage": 15, "role": "cashier"})
        cu = client.post("/api/users", headers=h, json={"username": "rem.cashier",
                         "name": "Cash Rem", "role": "cashier",
                         "employee_id": "RM-CASH"}).json()
        pw = cu["temp_password"]                 # generated once; use it to sign in
        ch = client.post("/api/auth/login",
                         data={"username": cu["username"], "password": pw})
        assert ch.status_code == 200
        ct = {"Authorization": "Bearer " + ch.json()["access_token"]}
        assert client.get("/api/telegram/reminders/settings", headers=ct).status_code == 403
        assert client.put("/api/telegram/reminders/settings", headers=ct,
                          json={"enabled": True}).status_code == 403
        # owner can
        assert client.get("/api/telegram/reminders/settings", headers=_tok()).status_code == 200


def test_unauthenticated_and_bot_endpoints_are_guarded():
    with TestClient(app):
        assert client.get("/api/telegram/reminders/settings").status_code == 401
        # worker endpoints require the bot token
        assert client.post("/api/telegram/reminders/claim").status_code == 403
        assert client.post("/api/telegram/reminders/claim", headers=_bot()).status_code == 200


# --------------------------------------------------------------- enable/disable
def test_enable_sets_next_run_disable_clears_it():
    with TestClient(app):
        off = client.put("/api/telegram/reminders/settings", headers=_tok(),
                         json={"enabled": False}).json()
        assert off["enabled"] is False and off["next_run_utc"] is None
        on = client.put("/api/telegram/reminders/settings", headers=_tok(),
                        json={"enabled": True}).json()
        assert on["enabled"] is True and on["next_run_utc"], "enabling schedules a run"
        assert on["next_run_local"], "next run is shown in the business timezone"


# ------------------------------------------------------------------- interval
def test_interval_change_recomputes_slots():
    with TestClient(app):
        r12 = client.put("/api/telegram/reminders/settings", headers=_tok(),
                         json={"enabled": True, "interval_hours": 12,
                               "active_start_hour": 8, "active_end_hour": 22}).json()
        assert r12["slot_hours"] == [8, 20]        # every 12h between 08:00 and 22:00
        r6 = client.put("/api/telegram/reminders/settings", headers=_tok(),
                        json={"interval_hours": 6}).json()
        assert r6["slot_hours"] == [8, 14, 20]
        r24 = client.put("/api/telegram/reminders/settings", headers=_tok(),
                         json={"interval_hours": 24}).json()
        assert r24["slot_hours"] == [8]
        # out-of-range interval is rejected
        bad = client.put("/api/telegram/reminders/settings", headers=_tok(),
                         json={"interval_hours": 0})
        assert bad.status_code == 422


def test_message_edit_persists():
    with TestClient(app):
        custom = "🔔 Custom reminder — please update the ERP."
        r = client.put("/api/telegram/reminders/settings", headers=_tok(),
                       json={"message": custom}).json()
        assert r["message"] == custom
        # empty message falls back to the default rather than sending nothing
        r2 = client.put("/api/telegram/reminders/settings", headers=_tok(),
                        json={"message": "   "}).json()
        assert r2["message"] == RM.DEFAULT_MESSAGE


# ------------------------------------------------------------------- timezone
def test_next_run_uses_business_timezone_and_active_window():
    with TestClient(app):
        r = client.put("/api/telegram/reminders/settings", headers=_tok(),
                       json={"enabled": True, "interval_hours": 12,
                             "active_start_hour": 8, "active_end_hour": 22}).json()
        db = SessionLocal()
        try:
            import app.reports_tg as R
            assert r["timezone"] == R.company_tz(db)
            local, _ = R.now_local(db)
            s = db.get(models.ReminderSetting, 1)
            nxt = RM.compute_next_run(local, s)
            # the next fire is one of the configured local slot hours (08:00/20:00)
            from zoneinfo import ZoneInfo
            nl = nxt.astimezone(ZoneInfo(R.company_tz(db)))
            assert nl.hour in (8, 20) and nl.minute == 0
            assert nxt > datetime.now(timezone.utc)
        finally:
            db.close()


# ------------------------------------------------------------------- scheduler
def test_scheduler_claims_when_due_and_advances():
    with TestClient(app):
        client.put("/api/telegram/reminders/settings", headers=_tok(),
                   json={"enabled": True, "recipient_mode": "all",
                         "active_start_hour": 0, "active_end_hour": 23, "paused_days": []})
        _force_due()
        first = client.post("/api/telegram/reminders/claim", headers=_bot()).json()
        assert first["claimed"] is True and first["skipped"] is False
        assert len(first["recipients"]) >= 1               # linked accounts receive it
        assert all(r["idem_key"] for r in first["recipients"])
        # schedule advanced, so an immediate second claim is not due
        second = client.post("/api/telegram/reminders/claim", headers=_bot()).json()
        assert second["claimed"] is False and second["reason"] == "not_due"
        # complete one delivery → it shows as sent in the log
        key = first["recipients"][0]["idem_key"]
        client.post("/api/telegram/reminders/complete", headers=_bot(),
                    json={"idem_key": key, "status": "sent", "message_id": "555"})
        log = client.get("/api/telegram/reminders/deliveries", headers=_tok()).json()["deliveries"]
        row = [d for d in log if d["message_id"] == "555"]
        assert row and row[0]["status"] == "sent" and row[0]["kind"] == "scheduled"


def test_disabled_scheduler_does_not_fire():
    with TestClient(app):
        client.put("/api/telegram/reminders/settings", headers=_tok(), json={"enabled": False})
        _force_due()                                       # even if a stale time exists
        r = client.post("/api/telegram/reminders/claim", headers=_bot()).json()
        assert r["claimed"] is False and r["reason"] == "disabled"


def test_paused_day_is_skipped_and_logged():
    with TestClient(app):
        import app.reports_tg as R
        db = SessionLocal()
        try:
            local, _ = R.now_local(db)
            today = local.weekday()
        finally:
            db.close()
        client.put("/api/telegram/reminders/settings", headers=_tok(),
                   json={"enabled": True, "active_start_hour": 0, "active_end_hour": 23,
                         "paused_days": [today]})
        _force_due()
        r = client.post("/api/telegram/reminders/claim", headers=_bot()).json()
        assert r["claimed"] is True and r["skipped"] is True
        log = client.get("/api/telegram/reminders/deliveries", headers=_tok()).json()["deliveries"]
        assert any(d["status"] == "skipped" for d in log)


def test_selected_mode_with_no_recipients_is_skipped():
    with TestClient(app):
        client.put("/api/telegram/reminders/settings", headers=_tok(),
                   json={"enabled": True, "active_start_hour": 0, "active_end_hour": 23,
                         "paused_days": [], "recipient_mode": "selected",
                         "recipient_ids": []})
        _force_due()
        r = client.post("/api/telegram/reminders/claim", headers=_bot()).json()
        assert r["claimed"] is True and r["skipped"] is True and r["reason"] == "no recipients"


def test_selected_mode_narrows_recipients():
    with TestClient(app):
        client.put("/api/telegram/reminders/settings", headers=_tok(),
                   json={"enabled": True, "active_start_hour": 0, "active_end_hour": 23,
                         "paused_days": [], "recipient_mode": "selected",
                         "recipient_ids": ["82001"]})
        _force_due()
        r = client.post("/api/telegram/reminders/claim", headers=_bot()).json()
        assert r["skipped"] is False
        ids = {x["tg_id"] for x in r["recipients"]}
        assert ids == {"82001"}, "only the selected account receives the reminder"


# --------------------------------------------------------------------- send-now
def test_send_now_queues_manual_batch_for_the_worker():
    with TestClient(app):
        client.put("/api/telegram/reminders/settings", headers=_tok(),
                   json={"enabled": True, "recipient_mode": "all"})
        q = client.post("/api/telegram/reminders/send-now", headers=_tok(), json={}).json()
        assert q["queued"] >= 1
        pend = client.get("/api/telegram/reminders/pending", headers=_bot()).json()["pending"]
        assert len(pend) >= 1 and all(p["message"] for p in pend)
        # the worker completes each
        key = pend[0]["idem_key"]
        assert key.startswith("manual|")
        client.post("/api/telegram/reminders/complete", headers=_bot(),
                    json={"idem_key": key, "status": "sent"})
        pend2 = client.get("/api/telegram/reminders/pending", headers=_bot()).json()["pending"]
        assert key not in {p["idem_key"] for p in pend2}, "sent items leave the pending queue"


# --------------------------------------------------------- restart persistence
def test_settings_survive_a_restart():
    with TestClient(app):
        client.put("/api/telegram/reminders/settings", headers=_tok(),
                   json={"enabled": True, "interval_hours": 8, "message": "Persisted msg",
                         "active_start_hour": 9, "active_end_hour": 21, "paused_days": [5, 6]})
    # a brand-new client + app context reads the SAME database row (no in-memory
    # schedule) — exactly what happens after a Render restart or redeploy.
    with TestClient(app):
        r = client.get("/api/telegram/reminders/settings", headers=_tok()).json()
        assert r["enabled"] is True
        assert r["interval_hours"] == 8
        assert r["message"] == "Persisted msg"
        assert r["active_start_hour"] == 9 and r["active_end_hour"] == 21
        assert r["paused_days"] == [5, 6]
        assert r["next_run_utc"], "the next run is still scheduled after a restart"


def test_idempotency_no_double_log_for_same_slot():
    with TestClient(app):
        client.put("/api/telegram/reminders/settings", headers=_tok(),
                   json={"enabled": True, "active_start_hour": 0, "active_end_hour": 23,
                         "paused_days": [], "recipient_mode": "selected",
                         "recipient_ids": ["82001"]})
        _force_due()
        first = client.post("/api/telegram/reminders/claim", headers=_bot()).json()
        key = first["recipients"][0]["idem_key"]
        # re-inserting the same ledger key must be rejected (unique idem_key)
        db = SessionLocal()
        try:
            dup = models.ReminderDelivery(idem_key=key, run_at=datetime.now(timezone.utc),
                                          kind="scheduled", tg_id="82001", status="queued")
            db.add(dup)
            raised = False
            try:
                db.commit()
            except Exception:
                raised = True
                db.rollback()
            assert raised, "duplicate idem_key must violate the unique constraint"
        finally:
            db.close()
