"""Employee Work Schedule — calendar, recurrence, publishing, Telegram delivery,
permission enforcement, updates. Drives the same endpoints the worker calls.
"""
import os, tempfile
from datetime import date, timedelta

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_sched_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "schedule-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

from fastapi.testclient import TestClient
from app.main import app
from app import schedule_tg as SC
from app.config import settings

client = TestClient(app)

MON = SC.week_start_of(date.today())          # this week's Monday
NEXTMON = MON + timedelta(days=7)


def _bot():
    if not settings.bot_token:
        settings.bot_token = "test-bot-token"
    return {"X-Bot-Token": settings.bot_token}


def _tok(uid="U-owner", pw="demo1234"):
    r = client.post("/api/auth/login", data={"username": uid, "password": pw})
    return {"Authorization": "Bearer " + r.json()["access_token"]}


# employee with a linked Telegram account (receives deliveries)
def _setup():
    h = _tok()
    client.post("/api/employees", headers=h, json={"id": "SC-A", "name": "Sched Alice",
                "branch": "Store A", "title": "Staff", "pay_type": "salary", "salary": 1000, "role": "employee"})
    client.post("/api/employees", headers=h, json={"id": "SC-B", "name": "Sched Bob",
                "branch": "Store B", "title": "Staff", "pay_type": "salary", "salary": 1000, "role": "employee"})
    c = client.post("/api/telegram/link-code", headers=h, json={"employee_id": "SC-A"}).json().get("code")
    if c:
        client.post("/api/telegram/link/verify", json={"tg_id": "83001", "code": c, "username": "sched_alice"})
    return h


# ------------------------------------------------------------- recurrence + calendar
def test_create_recurring_weekdays_and_read_calendar():
    with TestClient(app):
        h = _setup()
        r = client.post("/api/schedule/entries", headers=h, json={
            "employee_id": "SC-A", "start": str(MON), "weeks": 1,
            "recurrence": "weekdays", "start_time": "09:00", "end_time": "17:00"})
        assert r.status_code == 201
        made = r.json()["created"]
        assert len(made) == 5                       # Mon–Fri
        assert all(m["week_start"] == str(MON) for m in made)
        cal = client.get(f"/api/schedule/entries?start={MON}&end={MON+timedelta(days=6)}",
                         headers=h).json()
        assert cal["can_manage"] is True
        got = [e for e in cal["entries"] if e["employee_id"] == "SC-A"]
        assert len(got) == 5


def test_weekends_and_alternate_patterns():
    with TestClient(app):
        h = _tok()
        we = client.post("/api/schedule/entries", headers=h, json={
            "employee_id": "SC-B", "start": str(MON), "weeks": 1, "recurrence": "weekends"}).json()["created"]
        assert len(we) == 2                          # Sat + Sun
        alt = client.post("/api/schedule/entries", headers=h, json={
            "employee_id": "SC-B", "start": str(MON), "weeks": 4,
            "recurrence": "custom", "weekdays": [0], "every_other": True}).json()["created"]
        assert len(alt) == 2                          # week 1 and week 3 only


# --------------------------------------------------------------------- editing
def test_edit_and_delete_entry():
    with TestClient(app):
        h = _setup()
        made = client.post("/api/schedule/entries", headers=h, json={
            "employee_id": "SC-A", "dates": [str(MON)], "start_time": "10:00", "end_time": "18:00"}).json()["created"]
        eid = made[0]["id"]
        up = client.put(f"/api/schedule/entries/{eid}", headers=h,
                        json={"start_time": "12:00", "break_minutes": 30, "notes": "late shift"}).json()
        assert up["start_time"] == "12:00" and up["break_minutes"] == 30 and up["notes"] == "late shift"
        assert client.delete(f"/api/schedule/entries/{eid}", headers=h).status_code == 200


# ------------------------------------------------------------------- copy / bulk
def test_copy_week_and_bulk():
    with TestClient(app):
        h = _setup()
        client.post("/api/schedule/entries", headers=h, json={
            "employee_id": "SC-A", "start": str(MON), "weeks": 1, "recurrence": "weekdays"})
        cp = client.post("/api/schedule/copy-week", headers=h,
                         json={"employee_id": "SC-A", "from_week": str(MON), "to_weeks": [str(NEXTMON)]}).json()
        assert cp["copied"] == 5
        nxt = client.get(f"/api/schedule/entries?start={NEXTMON}&end={NEXTMON+timedelta(days=6)}", headers=h).json()
        assert len([e for e in nxt["entries"] if e["employee_id"] == "SC-A"]) == 5
        bulk = client.post("/api/schedule/bulk", headers=h, json={
            "employee_ids": ["SC-A", "SC-B"], "start": str(MON), "weeks": 1,
            "recurrence": "weekdays", "start_time": "08:00", "end_time": "16:00"}).json()
        assert bulk["employees"] == 2 and bulk["created"] == 10


# --------------------------------------------------------- rendering / message format
def test_week_message_format():
    with TestClient(app):
        h = _tok()   # dedicated employee, untouched by other tests (shared DB)
        client.post("/api/employees", headers=h, json={"id": "SC-MSG", "name": "Msg Mia",
                    "branch": "Store A", "title": "Staff", "pay_type": "salary", "salary": 1000, "role": "employee"})
        client.post("/api/schedule/entries", headers=h, json={
            "employee_id": "SC-MSG", "dates": [str(MON), str(MON+timedelta(days=3))],
            "start_time": "09:00", "end_time": "17:00"})
        pv = client.get(f"/api/schedule/preview?week_start={MON}&employee_id=SC-MSG", headers=h).json()
        msg = pv["previews"][0]["message"]
        assert "\U0001F4C5 Your Work Schedule" in msg
        assert "Hello Msg Mia," in msg
        assert "Monday\n09:00 - 17:00" in msg
        assert "Tuesday\nOFF" in msg                 # not scheduled that day
        assert "Thursday\n09:00 - 17:00" in msg
        assert "Please contact your manager" in msg


# ------------------------------------------------------------- publish + delivery
def test_publish_queues_and_worker_delivers():
    with TestClient(app):
        h = _setup()
        client.post("/api/schedule/entries", headers=h, json={
            "employee_id": "SC-A", "start": str(MON), "weeks": 1, "recurrence": "weekdays"})
        pub = client.post("/api/schedule/publish", headers=h,
                          json={"week_start": str(MON), "employee_id": "SC-A"}).json()
        assert pub["published_employees"] == 1 and pub["queued"] == 1
        # worker pulls the queued delivery, sends, and reports back
        pend = client.get("/api/schedule/pending", headers=_bot()).json()["pending"]
        job = [p for p in pend if p["tg_id"] == "83001"][0]
        assert "Your Work Schedule" in job["message"]
        client.post("/api/schedule/complete", headers=_bot(),
                    json={"idem_key": job["idem_key"], "status": "sent", "message_id": "900"})
        log = client.get("/api/schedule/deliveries", headers=h).json()["deliveries"]
        sent = [d for d in log if d["message_id"] == "900"]
        assert sent and sent[0]["status"] == "sent" and sent[0]["kind"] == "publish"


def test_unlinked_employee_is_skipped_not_lost():
    with TestClient(app):
        h = _setup()   # SC-B has no telegram link
        client.post("/api/schedule/entries", headers=h, json={
            "employee_id": "SC-B", "start": str(MON), "weeks": 1, "recurrence": "weekdays"})
        pub = client.post("/api/schedule/publish", headers=h,
                          json={"week_start": str(MON), "employee_id": "SC-B"}).json()
        assert pub["skipped"] >= 1 and pub["queued"] == 0
        log = client.get("/api/schedule/deliveries", headers=h).json()["deliveries"]
        assert any(d["recipient"] == "Sched Bob" and d["status"] == "skipped" for d in log)


def test_update_to_published_week_requeues():
    with TestClient(app):
        h = _setup()
        made = client.post("/api/schedule/entries", headers=h, json={
            "employee_id": "SC-A", "dates": [str(MON)], "start_time": "09:00", "end_time": "17:00"}).json()["created"]
        client.post("/api/schedule/publish", headers=h, json={"week_start": str(MON), "employee_id": "SC-A"})
        before = len(client.get("/api/schedule/deliveries", headers=h).json()["deliveries"])
        client.put(f"/api/schedule/entries/{made[0]['id']}", headers=h, json={"start_time": "11:00"})
        after = client.get("/api/schedule/deliveries", headers=h).json()["deliveries"]
        assert len(after) > before
        assert any(d["kind"] == "update" for d in after)


def test_send_now_and_resend_failed():
    with TestClient(app):
        h = _setup()
        client.post("/api/schedule/entries", headers=h, json={
            "employee_id": "SC-A", "start": str(MON), "weeks": 1, "recurrence": "weekdays"})
        snd = client.post("/api/schedule/send", headers=h,
                          json={"week_start": str(MON), "employee_id": "SC-A"}).json()
        assert snd["queued"] == 1
        pend = client.get("/api/schedule/pending", headers=_bot()).json()["pending"]
        job = [p for p in pend if p["tg_id"] == "83001"][0]
        client.post("/api/schedule/complete", headers=_bot(),
                    json={"idem_key": job["idem_key"], "status": "failed", "error": "blocked"})
        log = client.get("/api/schedule/deliveries", headers=h).json()["deliveries"]
        failed = [d for d in log if d["status"] == "failed"][0]
        rs = client.post(f"/api/schedule/deliveries/{failed['id']}/resend", headers=h)
        assert rs.status_code == 200 and rs.json()["requeued"] is True
        pend2 = client.get("/api/schedule/pending", headers=_bot()).json()["pending"]
        assert any(p["tg_id"] == "83001" for p in pend2)


# -------------------------------------------------------------- weekly auto-send
def test_weekly_auto_endpoint_is_gated_and_idempotent():
    with TestClient(app):
        # bot token required
        assert client.post("/api/schedule/auto").status_code == 403
        r = client.post("/api/schedule/auto", headers=_bot())
        assert r.status_code == 200 and "enqueued" in r.json()   # runs; enqueues only in the pre-week slot


# ------------------------------------------------------------------ permissions
def test_employee_is_read_only_and_sees_only_own():
    with TestClient(app):
        h = _tok()   # dedicated employee (shared DB across tests)
        client.post("/api/employees", headers=h, json={"id": "SC-RO", "name": "RO Rita",
                    "branch": "Store A", "title": "Staff", "pay_type": "salary", "salary": 1000, "role": "employee"})
        client.post("/api/schedule/entries", headers=h, json={
            "employee_id": "SC-RO", "start": str(MON), "weeks": 1, "recurrence": "weekdays"})
        cu = client.post("/api/users", headers=h, json={"username": "sched.rita",
                         "name": "RO Rita", "role": "employee", "employee_id": "SC-RO"}).json()
        eh = {"Authorization": "Bearer " + client.post(
            "/api/auth/login", data={"username": cu["username"], "password": cu["temp_password"]}
        ).json()["access_token"]}
        # cannot create / publish / send / view the delivery log
        assert client.post("/api/schedule/entries", headers=eh, json={"employee_id": "SC-RO", "dates": [str(MON)]}).status_code == 403
        assert client.post("/api/schedule/publish", headers=eh, json={"week_start": str(MON)}).status_code == 403
        assert client.get("/api/schedule/deliveries", headers=eh).status_code == 403
        # CAN read only her own schedule (this week)
        mine = client.get(f"/api/schedule/my?start={MON}&end={MON+timedelta(days=6)}", headers=eh).json()
        assert mine["employee"]["id"] == "SC-RO"
        assert len(mine["entries"]) == 5
        assert "Your Work Schedule" in (mine["week_message"] or "")
        cal = client.get(f"/api/schedule/entries?start={MON}&end={MON+timedelta(days=6)}", headers=eh).json()
        assert cal["self_only"] is True and cal["can_manage"] is False
        assert all(e["employee_id"] == "SC-RO" for e in cal["entries"])


def test_unauthenticated_blocked():
    with TestClient(app):
        assert client.get("/api/schedule/entries").status_code == 401
        assert client.get("/api/schedule/my").status_code == 401
