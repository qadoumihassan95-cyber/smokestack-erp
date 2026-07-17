"""v3 feature tests: expense Other/custom_description, payroll without employee tax,
licenses + alert buckets, KPI costs/profit by period + permission gating, analytics,
comparisons/forecast, attendance worksheet."""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_v3_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "test-secret"

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def _tok(uid="U-owner"):
    r = client.post("/api/auth/login", data={"username": uid, "password": "demo1234"})
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def test_expense_other_requires_description():
    with TestClient(app):
        h = _tok("U-owner")
        # Other without description -> 422
        r = client.post("/api/expenses", json={"branch": "Store A", "category": "Other", "amount": 40}, headers=h)
        assert r.status_code == 422
        # Other with description -> stored and echoed as label
        r2 = client.post("/api/expenses", json={"branch": "Store A", "category": "Other",
                                                "amount": 40, "custom_description": "Cleaning materials"}, headers=h)
        assert r2.status_code == 201, r2.text
        d = r2.json()
        assert d["custom_description"] == "Cleaning materials"
        assert d["label"] == "Cleaning materials" and d["category"] == "Other"
        # persists + visible in list
        lst = client.get("/api/expenses?branch=all", headers=h).json()
        assert any(x.get("custom_description") == "Cleaning materials" for x in lst)


def test_normal_expense_has_no_custom_desc():
    with TestClient(app):
        h = _tok("U-owner")
        r = client.post("/api/expenses", json={"branch": "Store A", "category": "Utilities", "amount": 55}, headers=h)
        d = r.json()
        assert d["custom_description"] is None and d["label"] == "Utilities"


def test_payroll_has_no_employee_tax():
    with TestClient(app):
        r = client.get("/api/payroll?start=2026-07-01&end=2026-07-31&branch=all", headers=_tok("U-owner"))
        assert r.status_code == 200, r.text
        d = r.json()
        assert "employer_tax" not in d
        for row in d["rows"]:
            assert "tax" not in row
            assert row["net"] == row["gross"]     # net equals gross (no employee tax)
        assert d["total_cost"] == d["gross"]


def test_licenses_crud_and_alerts():
    with TestClient(app):
        h = _tok("U-owner")
        lst = client.get("/api/licenses", headers=h).json()
        assert len(lst) >= 5
        # seeded tobacco permit expires in ~25 days -> bucket d30
        assert any(x["doc_type"] == "tobacco_license" and x["bucket"] in ("d30", "d7") for x in lst)
        # create
        c = client.post("/api/licenses", json={"name": "Lease Agreement", "doc_type": "lease",
                                               "branch": "Store A", "expiry_date": "2027-01-01"}, headers=h)
        assert c.status_code == 201
        lid = c.json()["id"]
        # alerts include expired + critical
        al = client.get("/api/licenses/alerts", headers=h).json()
        assert al["count"] >= 1 and al["expired"] >= 1
        # edit + delete
        e = client.put(f"/api/licenses/{lid}", json={"name": "Lease A (renewed)"}, headers=h)
        assert e.status_code == 200 and e.json()["name"] == "Lease A (renewed)"
        d = client.delete(f"/api/licenses/{lid}", headers=h)
        assert d.status_code == 200


def test_kpi_costs_profit_and_gating():
    with TestClient(app):
        # owner sees both
        r = client.get("/api/reports/kpi?period=month&branch=all", headers=_tok("U-owner")).json()
        assert "costs" in r and "profit" in r
        assert "current" in r["costs"] and "delta_pct" in r["profit"]
        assert r["costs"]["breakdown"]["payroll"] is not None
        # employee cannot see costs or profit
        e = client.get("/api/reports/kpi?period=today&branch=all", headers=_tok("U-emp")).json()
        assert "costs" not in e and "profit" not in e
        assert e["can_view_costs"] is False and e["can_view_profit"] is False


def test_kpi_all_periods():
    with TestClient(app):
        for p in ("today", "week", "month", "year"):
            r = client.get(f"/api/reports/kpi?period={p}&branch=all", headers=_tok("U-owner"))
            assert r.status_code == 200 and r.json()["period"] == p


def test_analytics_shapes():
    with TestClient(app):
        a = client.get("/api/reports/analytics?branch=all", headers=_tok("U-owner")).json()
        for k in ("profit_trend", "costs_trend", "branch_comparison", "expenses_by_category",
                  "best_products", "peak_hours", "inventory_movement", "low_stock"):
            assert k in a, k
        assert len(a["profit_trend"]) == 6
        assert isinstance(a["low_stock"]["out"], int)


def test_comparisons_forecast_labeled():
    with TestClient(app):
        c = client.get("/api/reports/comparisons?branch=all", headers=_tok("U-owner")).json()
        for k in ("week_over_week", "month_over_month", "year_over_year", "forecast", "recommendations"):
            assert k in c
        assert "disclaimer" in c["forecast"] and "not a guaranteed" in c["forecast"]["disclaimer"].lower()
        assert isinstance(c["recommendations"], list) and c["recommendations"]


def test_worksheet_schedule_join():
    with TestClient(app):
        h = _tok("U-owner")
        # create an attendance record via clock-in near Store A (seeded coords), then read worksheet
        client.put("/api/attendance/branch/Store A", headers=h,
                   json={"lat": 32.2211, "lng": 35.2544, "radius_m": 150})
        client.post("/api/attendance/clock-in", json={"lat": 32.2213, "lng": 35.2544, "live": True}, headers=h)
        w = client.get("/api/attendance/worksheet?period=month&branch=all", headers=h).json()
        assert isinstance(w, list) and len(w) >= 1
        row = w[0]
        for k in ("employee", "branch", "sched_start", "sched_end", "clock_in",
                  "late_minutes", "overtime_minutes", "status", "distance", "source"):
            assert k in row, k
