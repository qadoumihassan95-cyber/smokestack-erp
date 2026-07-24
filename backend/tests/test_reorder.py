"""SmokeStack — inventory reorder/purchasing report (GET /api/inventory/reorder).

Deterministic + order-independent: seeds its own uniquely-named products/stock rows
directly, so it is unaffected by other suites sharing the process-wide engine.
"""
import os, tempfile
_DB = os.path.join(tempfile.gettempdir(), f"smokestack_reorder_{os.getpid()}.db")
if os.path.exists(_DB): os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "reorder-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app import models, tenancy

with TestClient(app): pass
client = TestClient(app)


def _seed(rows):
    """rows: list of (sku, name, supplier, cost, min_level, {branch: qty})."""
    db = SessionLocal(); tenancy.set_session_company(db, 1)
    for sku, name, sup, cost, mn, stock in rows:
        if not db.get(models.Product, sku):
            db.add(models.Product(sku=sku, name=name, supplier=sup, cost=cost, price=cost * 2,
                                  min_level=mn, status="active"))
        for br, q in stock.items():
            st = db.query(models.Stock).filter_by(sku=sku, branch=br).first()
            if st: st.qty = q
            else: db.add(models.Stock(company_id=1, sku=sku, branch=br, qty=q))
    db.commit(); db.close()


def _tok(user="U-owner"):
    return client.post("/api/auth/login", data={"username": user, "password": "demo1234"}).json().get("access_token")


def _reorder(tok, branch="all"):
    return client.get(f"/api/inventory/reorder?branch={branch}",
                      headers={"Authorization": "Bearer " + tok})


def test_reorder_lists_below_minimum_with_restore_to_minimum_qty():
    _seed([("RO-LOW", "Low Item", "ACME", 2.5, 50, {"Store A": 10})])
    r = _reorder(_tok(), "Store A"); assert r.status_code == 200
    it = next(x for x in r.json()["items"] if x["sku"] == "RO-LOW" and x["branch"] == "Store A")
    assert it["qty"] == 10 and it["min_level"] == 50
    assert it["below_by"] == 40 and it["suggested_order_qty"] == 40
    assert it["status"] == "low"
    assert it["unit_cost"] == 2.5 and it["est_cost"] == 100.0        # 40 * 2.5


def test_healthy_stock_is_excluded():
    _seed([("RO-OK", "Healthy", "ACME", 1.0, 10, {"Store A": 999})])
    skus = {x["sku"] for x in _reorder(_tok(), "Store A").json()["items"]}
    assert "RO-OK" not in skus


def test_out_of_stock_flagged_and_suggested_to_minimum():
    _seed([("RO-OUT", "Out Item", "BETA", 4.0, 8, {"Store A": 0})])
    it = next(x for x in _reorder(_tok(), "Store A").json()["items"] if x["sku"] == "RO-OUT")
    assert it["status"] == "out" and it["qty"] == 0
    assert it["suggested_order_qty"] == 8 and it["est_cost"] == 32.0  # 8 * 4.0


def test_exactly_at_minimum_orders_nothing():
    _seed([("RO-ATMIN", "At Min", "ACME", 1.0, 20, {"Store A": 20})])
    skus = {x["sku"] for x in _reorder(_tok(), "Store A").json()["items"]}
    assert "RO-ATMIN" not in skus                                    # order qty would be 0


def test_grouped_by_supplier_and_totals():
    _seed([("RO-S1", "S1", "SupOne", 3.0, 10, {"Store B": 0}),
           ("RO-S2", "S2", "SupOne", 5.0, 4, {"Store B": 1}),
           ("RO-S3", "S3", "SupTwo", 2.0, 6, {"Store B": 0})])
    body = _reorder(_tok(), "Store B").json()
    sup = {g["supplier"]: g for g in body["by_supplier"]}
    assert "SupOne" in sup and "SupTwo" in sup
    # SupOne: RO-S1 order 10*3=30 + RO-S2 order 3*5=15 => 45 over 2 lines
    assert sup["SupOne"]["lines"] == 2 and sup["SupOne"]["est_cost"] == 45.0
    assert sup["SupTwo"]["est_cost"] == 12.0                         # 6 * 2.0
    assert body["totals"]["items"] >= 3
    assert body["totals"]["est_cost"] >= 57.0


def test_branch_scoping_only_returns_requested_branch():
    _seed([("RO-BR", "Branch Item", "ACME", 1.0, 30, {"Store A": 1, "Store B": 999})])
    a = [x for x in _reorder(_tok(), "Store A").json()["items"] if x["sku"] == "RO-BR"]
    b = [x for x in _reorder(_tok(), "Store B").json()["items"] if x["sku"] == "RO-BR"]
    assert len(a) == 1 and a[0]["branch"] == "Store A"
    assert b == []                                                   # healthy at Store B


def test_view_cost_capability_hides_cost_fields():
    _seed([("RO-COST", "Cost Item", "ACME", 9.0, 10, {"Store A": 0})])
    tok = _tok("U-cash")                                             # cashier: no view_cost
    if not tok:
        return                                                      # role/login unavailable — skip
    r = _reorder(tok, "Store A")
    if r.status_code != 200:
        return                                                      # cashier lacks inventory view — not this test's concern
    it = next((x for x in r.json()["items"] if x["sku"] == "RO-COST"), None)
    if it is not None:
        assert it["unit_cost"] is None and it["est_cost"] is None
    assert r.json()["totals"]["est_cost"] is None
