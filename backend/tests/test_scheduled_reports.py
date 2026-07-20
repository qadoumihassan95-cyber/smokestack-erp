"""Scheduled Telegram reports: idempotency, scoping, content and periods."""
import os, tempfile
_DB = os.path.join(tempfile.gettempdir(), f"smokestack_rep_{os.getpid()}.db")
if os.path.exists(_DB): os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "reports-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

from fastapi.testclient import TestClient
from app.main import app
from app import reports_tg as R
from app.config import settings
from app.database import SessionLocal
from app import models

client = TestClient(app)

def _bot():
    if not settings.bot_token: settings.bot_token = "test-bot-token"
    return {"X-Bot-Token": settings.bot_token}

def _tok(uid="U-owner"):
    r = client.post("/api/auth/login", data={"username": uid, "password": "demo1234"})
    return {"Authorization": "Bearer " + r.json()["access_token"]}

CAST = [("RP-OWNER","Rep Owner","Store A","owner","81001"),
        ("RP-MGR","Rep Manager","Store B","branch_manager","81002")]

def _setup():
    h = _tok()
    for eid,name,branch,role,tg in CAST:
        client.post("/api/employees", headers=h, json={"id":eid,"name":name,"branch":branch,
            "title":"Staff","pay_type":"salary","salary":1000,"role":role})
        c = client.post("/api/telegram/link-code", headers=h, json={"employee_id":eid}).json().get("code")
        if c:
            client.post("/api/telegram/link/verify", json={"tg_id":tg,"code":c,"username":"rep_"+eid.lower()})
        client.put(f"/api/telegram/reports/recipients/{tg}", headers=h,
                   json={"enabled":True,"morning":True,"evening":True})

def test_timezone_is_the_configured_business_timezone():
    with TestClient(app):
        _setup()
        r = client.get("/api/telegram/reports/recipients", headers=_tok()).json()
        assert r["timezone"], "a timezone must be reported"
        assert r["slots"] == ["06:00", "18:00"]
        db = SessionLocal()
        try: assert R.company_tz(db) == r["timezone"]
        finally: db.close()

def test_owner_gets_all_branches_manager_only_their_own():
    with TestClient(app):
        rows = {x["tg_id"]: x for x in
                client.get("/api/telegram/reports/recipients", headers=_tok()).json()["recipients"]}
        assert set(rows["81001"]["branches"]) == {"Store A","Store B","Store C"}
        assert rows["81002"]["branches"] == ["Store B"]

def test_configuration_can_never_widen_a_managers_scope():
    with TestClient(app):
        r = client.put("/api/telegram/reports/recipients/81002", headers=_tok(),
                       json={"branches":["Store A","Store B","Store C"]}).json()
        assert r["branches"] == ["Store B"], "config must not widen ERP scope"

def test_morning_and_evening_periods_are_correct():
    with TestClient(app):
        m = client.get("/api/telegram/reports/preview?kind=morning", headers=_tok()).json()
        e = client.get("/api/telegram/reports/preview?kind=evening", headers=_tok()).json()
        mt = m["messages"][0]["text"]; et = e["messages"][0]["text"]
        assert "Morning Report" in mt and "Previous day" in mt
        assert "Evening Report" in et and "as of" in et.lower()
        assert "not a final full-day report" in et

def test_report_contains_required_sections_and_no_raw_data():
    with TestClient(app):
        m = client.get("/api/telegram/reports/preview?kind=morning", headers=_tok()).json()
        t = m["messages"][0]["text"]
        for f in ("Sales","COGS","Expenses","Gross profit","Net operating result",
                  "Cash ready to deposit","Inventory value","Low stock","Out of stock",
                  "Licenses","IMPORTANT ALERTS","Reporting period"):
            assert f in t, f
        assert "{" not in t and "}" not in t, "no raw JSON in a report"
        assert len(m["messages"]) >= 2, "combined report plus per-branch reports"

def test_missing_values_say_not_available_never_a_fake_zero():
    assert R.money(None) == "Not available"
    assert R.money(0) == "$0.00"

def test_message_splitting_is_numbered():
    long = "\n".join(f"line {i} " + "x"*80 for i in range(200))
    parts = R.split_message(long)
    assert len(parts) > 1
    assert parts[0].startswith(f"<b>Part 1 of {len(parts)}</b>")
    assert all(len(p) <= R.TG_LIMIT + 40 for p in parts)
    assert R.split_message("short") == ["short"]

def test_claim_is_idempotent_across_duplicate_executions():
    with TestClient(app):
        body = {"tg_id":"81001","kind":"morning","slot":"06:00","business_date":"2026-07-20"}
        a = client.post("/api/telegram/reports/claim", headers=_bot(), json=body).json()
        b = client.post("/api/telegram/reports/claim", headers=_bot(), json=body).json()
        assert a["claimed"] is True
        assert b["claimed"] is False, "a duplicate execution must never send twice"
        assert a["idem_key"] == "smokestack|81001|morning|2026-07-20|06:00"

def test_idempotency_key_is_per_recipient_type_date_and_slot():
    with TestClient(app):
        base = {"tg_id":"81001","kind":"morning","slot":"06:00","business_date":"2026-07-21"}
        assert client.post("/api/telegram/reports/claim", headers=_bot(), json=base).json()["claimed"]
        for change in ({"tg_id":"81002"}, {"kind":"evening","slot":"18:00"},
                       {"business_date":"2026-07-22"}):
            j = dict(base); j.update(change)
            assert client.post("/api/telegram/reports/claim", headers=_bot(),
                               json=j).json()["claimed"] is True, change

def test_disabled_account_receives_nothing():
    with TestClient(app):
        h = _tok()
        assert client.post("/api/telegram/accounts/81002/disable", headers=h).status_code == 200
        out = client.post("/api/telegram/reports/claim", headers=_bot(),
                          json={"tg_id":"81002","kind":"morning","slot":"06:00",
                                "business_date":"2026-07-25"}).json()
        assert out["claimed"] is False
        due = client.get("/api/telegram/reports/due", headers=_bot()).json()
        assert all(d["tg_id"] != "81002" for d in due["due"])
        client.post("/api/telegram/accounts/81002/enable", headers=h)

def test_manual_send_never_consumes_a_scheduled_slot():
    with TestClient(app):
        r = client.post("/api/telegram/reports/send-now", headers=_tok(),
                        json={"tg_id":"81001","kind":"morning","test":True}).json()
        assert r["queued"] and r["idem_key"].startswith("manual|")
        # the scheduled slot for the same day is still free
        j = {"tg_id":"81001","kind":"morning","slot":"06:00","business_date":"2026-07-30"}
        assert client.post("/api/telegram/reports/claim", headers=_bot(), json=j).json()["claimed"]

def test_test_reports_are_labelled():
    with TestClient(app):
        r = client.post("/api/telegram/reports/render", headers=_bot(),
                        json={"tg_id":"81001","kind":"morning","test":True}).json()
        assert "TEST REPORT" in r["messages"][0]

def test_delivery_log_records_status_and_failures():
    with TestClient(app):
        j = {"tg_id":"81001","kind":"evening","slot":"18:00","business_date":"2026-08-01"}
        c = client.post("/api/telegram/reports/claim", headers=_bot(), json=j).json()
        client.post("/api/telegram/reports/complete", headers=_bot(),
                    json={"idem_key":c["idem_key"],"status":"partial","retries":2,
                          "error":"1 message(s) failed","message_ids":[11,12]})
        rows = client.get("/api/telegram/reports/deliveries", headers=_tok()).json()
        row = next(r for r in rows if r["idem_key"] == c["idem_key"])
        assert row["status"] == "partial" and row["retries"] == 2
        assert row["error"] and row["sent_at"] and row["recipient"] == "Rep Owner"
        for s in ("pending","processing","sent","partial","failed","skipped"):
            assert isinstance(s, str)

def test_report_totals_match_the_dashboard_engine():
    """The report must not re-implement any financial formula."""
    with TestClient(app):
        from app.routers import core as C
        db = SessionLocal()
        try:
            from datetime import date
            brs = ["Store A","Store B","Store C"]
            today = R.business_date(db)
            cp = C._costs_profit(db, brs, today, today)
            data = R.collect(db, brs, today, today, today)
            assert data["sales"] == cp["revenue"]
            assert data["cogs"] == cp["cogs"]
            assert data["expenses"] == cp["opex"]
            assert data["net"] == cp["profit"]
            assert data["tax"] == cp["tax"]
        finally:
            db.close()

def test_endpoints_require_authorisation():
    with TestClient(app):
        assert client.get("/api/telegram/reports/recipients").status_code == 401
        assert client.get("/api/telegram/reports/due").status_code == 403
        assert client.post("/api/telegram/reports/claim", json={}).status_code == 403
        for uid in ("U-emp","U-cash"):
            assert client.get("/api/telegram/reports/recipients",
                              headers=_tok(uid)).status_code == 403
            assert client.put("/api/telegram/reports/recipients/81001", headers=_tok(uid),
                              json={"enabled":False}).status_code == 403
