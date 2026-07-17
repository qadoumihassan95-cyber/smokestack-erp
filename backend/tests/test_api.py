"""End-to-end API tests via FastAPI TestClient against a fresh SQLite DB.
Covers auth, RBAC denials, branch scope, product CRUD, receive/adjust, the
ledger-based as-of math (independently verified), dashboard, ledger, approvals,
payroll, and the Telegram linking flow."""
import os, tempfile, importlib
from datetime import datetime, timedelta

# fresh temp DB before importing the app
_DB = os.path.join(tempfile.gettempdir(), f"smokestack_test_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "test-secret"

from fastapi.testclient import TestClient
from app.main import app
from app import models
from app.database import SessionLocal

client = TestClient(app)
PW = "demo1234"

def tok(uid):
    r = client.post("/api/auth/login", data={"username": uid, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}

def test_health():
    with TestClient(app) as c:  # triggers startup (create tables + seed)
        assert c.get("/api/health").json()["status"] == "ok"

def test_login_and_me():
    with TestClient(app):
        assert client.post("/api/auth/login", data={"username": "U-owner", "password": "wrong"}).status_code == 401
        h = tok("U-owner")
        me = client.get("/api/auth/me", headers=h).json()
        assert me["role"] == "owner"
        assert client.get("/api/auth/me").status_code == 401  # no token

def test_branch_scope():
    with TestClient(app):
        owner = client.get("/api/branches", headers=tok("U-owner")).json()
        cash = client.get("/api/branches", headers=tok("U-cash")).json()
        assert set(owner) == {"Store A", "Store B", "Store C"}
        assert cash == ["Store A"]  # cashier scoped to one branch

def test_product_crud_and_rbac():
    with TestClient(app):
        # cashier cannot create products
        r = client.post("/api/inventory/products", headers=tok("U-cash"),
                        json={"sku": "X1", "name": "Test", "price": 5})
        assert r.status_code == 403
        # owner can
        r = client.post("/api/inventory/products", headers=tok("U-owner"),
                        json={"sku": "X1", "name": "Test", "barcode": "999", "cost": 2, "price": 5, "min_level": 3})
        assert r.status_code == 201, r.text
        # duplicate sku rejected
        assert client.post("/api/inventory/products", headers=tok("U-owner"),
                           json={"sku": "X1", "name": "Dup"}).status_code == 409
        # search finds it
        got = client.get("/api/inventory/products?q=Test", headers=tok("U-owner")).json()
        assert any(p["sku"] == "X1" for p in got)

def test_receive_adjust_and_guards():
    with TestClient(app):
        # inventory_manager receives into Store A
        r = client.post("/api/inventory/receive", headers=tok("U-inv"),
                        json={"sku": "MRB-GLD", "branch": "Store A", "qty": 10})
        assert r.status_code == 200 and r.json()["ok"]
        # cashier cannot adjust
        assert client.post("/api/inventory/adjust", headers=tok("U-cash"),
                           json={"sku": "MRB-GLD", "branch": "Store A", "qty": -1, "reason": "x"}).status_code == 403
        # adjust requires a reason
        assert client.post("/api/inventory/adjust", headers=tok("U-inv"),
                           json={"sku": "MRB-GLD", "branch": "Store A", "qty": -1}).status_code == 422
        # inventory_manager cannot touch Store C (not in scope)
        assert client.post("/api/inventory/receive", headers=tok("U-inv"),
                           json={"sku": "MRB-GLD", "branch": "Store C", "qty": 5}).status_code == 403

def test_asof_math_independently_verified():
    with TestClient(app):
        db = SessionLocal()
        # wipe + control the ledger for one product/branch
        db.query(models.Movement).filter_by(sku="RAW-CLS", branch="Store A").delete()
        st = db.query(models.Stock).filter_by(sku="RAW-CLS", branch="Store A").first()
        run = 0
        for dt, ch in [("2025-01-05", 100), ("2025-02-10", 50), ("2025-03-01", -30), ("2025-06-15", 20)]:
            before = run; run += ch
            db.add(models.Movement(sku="RAW-CLS", branch="Store A", type="receive" if ch >= 0 else "sale",
                                   qty_before=before, qty_change=ch, qty_after=run, unit_cost=0.9, user_id="U-owner",
                                   moved_at=datetime.fromisoformat(dt + "T10:00:00")))
        st.qty = run
        db.commit(); db.close()
        from app.routers.inventory import as_of_qty
        db = SessionLocal()
        try:
            assert as_of_qty(db, "RAW-CLS", "Store A", "2025-01-04") == 0
            assert as_of_qty(db, "RAW-CLS", "Store A", "2025-01-05") == 100
            assert as_of_qty(db, "RAW-CLS", "Store A", "2025-02-28") == 150
            assert as_of_qty(db, "RAW-CLS", "Store A", "2025-03-01") == 120
            assert as_of_qty(db, "RAW-CLS", "Store A", "2025-12-31") == 140
            assert as_of_qty(db, "RAW-CLS", "Store A", "2025-04-01") == 120  # future move excluded
        finally:
            db.close()
        # and via the HTTP endpoint (permission-gated)
        rep = client.get("/api/inventory/asof?date=2025-02-15&branch=Store A", headers=tok("U-owner")).json()
        row = next(r for r in rep["rows"] if r["sku"] == "RAW-CLS")
        assert row["qty"] == 150
        # cashier lacks view_asof
        assert client.get("/api/inventory/asof?date=2025-02-15", headers=tok("U-cash")).status_code == 403

def test_dashboard_cost_visibility():
    with TestClient(app):
        d_owner = client.get("/api/reports/dashboard", headers=tok("U-owner")).json()
        assert "inventory_cost" in d_owner and d_owner["sales_today"] >= 0
        d_cash = client.get("/api/reports/dashboard", headers=tok("U-cash")).json()
        assert "inventory_cost" not in d_cash  # cashier can't see cost

def test_expense_and_purchase_flow():
    with TestClient(app):
        # employee (no create) blocked; cashier (has create) allowed
        assert client.post("/api/expenses", headers=tok("U-emp"),
                           json={"branch": "Store A", "category": "Fuel", "amount": 50}).status_code == 403
        assert client.post("/api/expenses", headers=tok("U-cash"),
                           json={"branch": "Store A", "category": "Fuel", "amount": 50}).status_code == 201
        # a purchase creates a pending approval
        r = client.post("/api/purchases", headers=tok("U-owner"),
                        json={"vendor": "Swedish Match", "branch": "Store A", "amount": 2100})
        assert r.status_code == 201
        pend = client.get("/api/approvals", headers=tok("U-owner")).json()
        assert any(a["kind"] == "purchase" for a in pend)

def test_approvals_and_payroll_permissions():
    with TestClient(app):
        assert client.get("/api/approvals", headers=tok("U-cash")).status_code == 403  # cashier can't approve
        assert client.get("/api/payroll?start=2025-01-01&end=2025-01-31", headers=tok("U-cash")).status_code == 403
        pr = client.get("/api/payroll?start=2025-01-01&end=2025-01-31", headers=tok("U-acct")).json()
        assert pr["gross"] >= 0 and "total_cost" in pr

def test_telegram_link_flow():
    with TestClient(app):
        h = tok("U-owner")
        code = client.post("/api/telegram/link/issue", headers=h).json()["code"]
        assert client.post("/api/telegram/link/verify", json={"tg_id": "111", "code": "000000"}).status_code == 400
        ok = client.post("/api/telegram/link/verify", json={"tg_id": "111", "code": code})
        assert ok.status_code == 200 and ok.json()["user"]["role"] == "owner"
        # one-time: reuse fails
        assert client.post("/api/telegram/link/verify", json={"tg_id": "222", "code": code}).status_code == 400
        assert client.get("/api/telegram/session/111").json()["linked"] is True

def test_audit_log_written():
    with TestClient(app):
        rows = client.get("/api/audit", headers=tok("U-owner")).json()
        assert any(a["action"] == "login" for a in rows)
        assert client.get("/api/audit", headers=tok("U-cash")).status_code == 403
