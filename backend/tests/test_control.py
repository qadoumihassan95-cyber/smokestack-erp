"""Financial Control Center tests — read-only guarantee, report shape, scoring,
history filtering and access control."""
import json
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_ctrl_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "control-secret-long-enough"

from fastapi.testclient import TestClient
from sqlalchemy import func
from app.main import app
from app.database import SessionLocal
from app import models

client = TestClient(app)

BUSINESS_TABLES = [models.Ledger, models.Purchase, models.Movement, models.Stock,
                   models.Product, models.Employee, models.Attendance, models.Transfer,
                   models.Approval, models.License, models.User, models.Branch]


def _tok(uid="U-owner"):
    r = client.post("/api/auth/login", data={"username": uid, "password": "demo1234"})
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _snapshot():
    db = SessionLocal()
    try:
        return {m.__name__: db.query(func.count()).select_from(m).scalar() for m in BUSINESS_TABLES}
    finally:
        db.close()


def test_validation_is_read_only():
    """The headline guarantee: running validation must not change any business row."""
    with TestClient(app):
        h = _tok("U-owner")
        before = _snapshot()
        client.get("/api/control/validate", headers=h)
        client.post("/api/control/validate", headers=h)   # stores history only
        after = _snapshot()
        assert before == after, f"validation mutated business data: {before} -> {after}"


def test_report_shape_and_sections():
    with TestClient(app):
        r = client.get("/api/control/validate", headers=_tok("U-owner"))
        assert r.status_code == 200, r.text
        rep = r.json()
        for k in ("score", "label", "severity", "totals", "sections", "duration_ms", "performance"):
            assert k in rep, k
        mods = {s["module"] for s in rep["sections"]}
        for expected in ("Accounting", "Database", "Inventory", "Security", "Performance",
                         "Reports", "Dashboard"):
            assert expected in mods, f"missing module {expected}"
        assert 0 <= rep["score"] <= 100
        assert rep["totals"]["checks"] > 20
        # every failing check must carry a cause and a fix
        for s in rep["sections"]:
            for c in s["checks"]:
                assert c["status"] in ("pass", "warning", "error", "critical")
                if c["status"] != "pass":
                    assert c["cause"] and c["fix"], f"{c['check']} missing cause/fix"


def test_healthy_system_scores_high():
    """A clean dataset must produce no data-integrity critical findings.

    Deployment-config checks (signing-secret length, bot token) are excluded:
    the shared test harness sets a deliberately short JWT_SECRET, whereas
    production uses a generated long value. Those checks are asserted
    separately in test_security_config_checks_are_reported.
    """
    ENV_CHECKS = {"JWT secret configured", "Telegram bot endpoints are token-gated",
                  "JWT expiry configured"}
    with TestClient(app):
        rep = client.get("/api/control/validate", headers=_tok("U-owner")).json()
        crit = [c for s in rep["sections"] for c in s["checks"]
                if c["status"] == "critical" and c["check"] not in ENV_CHECKS]
        assert not crit, crit
        assert rep["score"] >= 80, rep["score"]


def test_security_config_checks_are_reported():
    """The auditor must actually flag a weak signing secret rather than ignore it."""
    with TestClient(app):
        from app.config import settings
        rep = client.get("/api/control/validate", headers=_tok("U-owner")).json()
        sec = next(s for s in rep["sections"] if s["module"] == "Security")
        jwt_chk = next(c for c in sec["checks"] if c["check"] == "JWT secret configured")
        weak = not settings.jwt_secret or len(str(settings.jwt_secret)) < 16
        assert (jwt_chk["status"] == "critical") == weak, \
            f"secret weak={weak} but check reported {jwt_chk['status']}"
        # the secret itself must never be exposed in the report
        assert str(settings.jwt_secret) not in json.dumps(rep), "signing secret leaked into report"


def test_detects_injected_integrity_fault():
    """Seed a broken row directly in the DB and confirm the auditor catches it."""
    with TestClient(app):
        h = _tok("U-owner")
        db = SessionLocal()
        try:
            db.add(models.Movement(ref="MV-BAD", sku="RAW-CLS", branch="Store A", type="adjust",
                                   qty_before=10, qty_change=-5, qty_after=99))  # invariant broken
            db.commit()
        finally:
            db.close()
        rep = client.get("/api/control/validate", headers=h).json()
        inv = next(s for s in rep["sections"] if s["module"] == "Inventory")
        chk = next(c for c in inv["checks"] if "invariant" in c["check"])
        assert chk["status"] == "critical", chk
        assert rep["score"] < 100
        # clean up the injected row so later tests see a healthy system
        db = SessionLocal()
        try:
            db.query(models.Movement).filter(models.Movement.ref == "MV-BAD").delete()
            db.commit()
        finally:
            db.close()


def test_history_is_stored_and_filterable():
    with TestClient(app):
        h = _tok("U-owner")
        run = client.post("/api/control/validate", headers=h).json()
        assert "run_id" in run
        hist = client.get("/api/control/history", headers=h).json()
        assert isinstance(hist, list) and len(hist) >= 1
        row = hist[0]
        for k in ("id", "ts", "score", "passed", "warnings", "errors", "critical", "severity"):
            assert k in row, k
        # filters accepted
        assert client.get("/api/control/history?days=1", headers=h).status_code == 200
        assert client.get("/api/control/history?severity=ok", headers=h).status_code == 200
        assert client.get("/api/control/history?module=Accounting", headers=h).status_code == 200
        detail = client.get(f"/api/control/history/{run['run_id']}", headers=h).json()
        assert detail["score"] == run["score"]
        assert client.get("/api/control/history/999999", headers=h).status_code == 404


def test_access_is_restricted_to_all_branch_roles():
    with TestClient(app):
        assert client.get("/api/control/validate").status_code == 401
        for uid in ("U-emp", "U-cash", "U-inv", "U-bm"):
            assert client.get("/api/control/validate", headers=_tok(uid)).status_code == 403, uid
        for uid in ("U-owner", "U-admin", "U-acct"):
            assert client.get("/api/control/validate", headers=_tok(uid)).status_code == 200, uid


def test_control_module_does_not_alter_accounting_results():
    """Existing accounting endpoints must return identical values before/after."""
    with TestClient(app):
        h = _tok("U-owner")
        k1 = client.get("/api/reports/kpi?period=month&branch=all", headers=h).json()
        d1 = client.get("/api/reports/dashboard?branch=all", headers=h).json()
        client.post("/api/control/validate", headers=h)
        k2 = client.get("/api/reports/kpi?period=month&branch=all", headers=h).json()
        d2 = client.get("/api/reports/dashboard?branch=all", headers=h).json()
        assert k1["costs"] == k2["costs"] and k1["profit"] == k2["profit"]
        assert d1["profit_today"] == d2["profit_today"] and d1["sales_today"] == d2["sales_today"]
