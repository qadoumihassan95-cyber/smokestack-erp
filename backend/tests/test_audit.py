"""ERP AUDIT SUITE — accounting integrity, inventory ledger integrity, workflow
completion, and security. Each test asserts CORRECT behaviour; failures are bugs."""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_audit_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "audit-secret"

from datetime import date, timedelta
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app import models

client = TestClient(app)


def _tok(uid="U-owner"):
    r = client.post("/api/auth/login", data={"username": uid, "password": "demo1234"})
    return {"Authorization": "Bearer " + r.json()["access_token"]}


# ---------------------------------------------------------------- ACCOUNTING
def test_cogs_includes_purchases():
    """A purchase must count as cost of goods sold. Regression: purchases were
    written to the purchases table but COGS summed the ledger, so COGS was always 0."""
    with TestClient(app):
        h = _tok("U-owner")
        before = client.get("/api/reports/kpi?period=month&branch=all", headers=h).json()
        c0 = before["costs"]["breakdown"]["cogs"]
        r = client.post("/api/purchases", json={"vendor": "Acme Wholesale", "branch": "Store A",
                                                "amount": 1000}, headers=h)
        assert r.status_code == 201, r.text
        after = client.get("/api/reports/kpi?period=month&branch=all", headers=h).json()
        c1 = after["costs"]["breakdown"]["cogs"]
        assert c1 == c0 + 1000, f"COGS did not include the purchase: {c0} -> {c1}"
        # and total costs / profit must move accordingly
        assert after["costs"]["current"] == before["costs"]["current"] + 1000
        assert after["profit"]["current"] == before["profit"]["current"] - 1000


def test_dashboard_profit_matches_kpi_profit():
    """The dashboard widget and the KPI endpoint must report the same profit for
    the same period — reports must never disagree with each other."""
    with TestClient(app):
        h = _tok("U-owner")
        dash = client.get("/api/reports/dashboard?branch=all", headers=h).json()
        kpi = client.get("/api/reports/kpi?period=today&branch=all", headers=h).json()
        assert round(dash["profit_today"], 2) == round(kpi["profit"]["current"], 2), \
            f"dashboard {dash['profit_today']} != kpi {kpi['profit']['current']}"


def test_daily_report_matches_dashboard():
    with TestClient(app):
        h = _tok("U-owner")
        d = client.get("/api/reports/dashboard?branch=all", headers=h).json()
        rep = client.get("/api/reports/daily?branch=all", headers=h).json()
        rows = {r[0]: r[1] for r in rep["rows"]}
        assert round(rows["Sales"], 2) == round(d["sales_today"], 2)
        assert round(rows["Gross profit"], 2) == round(d["profit_today"], 2)


def test_sale_tax_cannot_exceed_amount():
    """Sales tax must never exceed the gross amount (data-integrity guard)."""
    with TestClient(app):
        h = _tok("U-owner")
        r = client.post("/api/sales", json={"branch": "Store A", "amount": 100, "tax": 500}, headers=h)
        assert r.status_code == 422, "tax greater than the sale amount must be rejected"


def test_negative_amounts_rejected():
    with TestClient(app):
        h = _tok("U-owner")
        assert client.post("/api/sales", json={"branch": "Store A", "amount": -50}, headers=h).status_code == 422
        assert client.post("/api/expenses", json={"branch": "Store A", "category": "Rent",
                                                  "amount": -5}, headers=h).status_code == 422


# ------------------------------------------------------- INVENTORY INTEGRITY
def test_stock_never_negative_and_ledger_invariant_holds():
    """Every movement must satisfy qty_before + qty_change == qty_after, and stock
    must never be silently clamped (which corrupted the immutable ledger)."""
    with TestClient(app):
        h = _tok("U-owner")
        client.post("/api/inventory/receive", json={"sku": "RAW-CLS", "branch": "Store A", "qty": 5}, headers=h)
        cur = next(p for p in client.get("/api/inventory/products?branch=all", headers=h).json()
                   if p["sku"] == "RAW-CLS")["stock"]["Store A"]
        # try to remove more than exists -> must be refused, not silently clamped
        r = client.post("/api/inventory/adjust",
                        json={"sku": "RAW-CLS", "branch": "Store A", "qty": -(cur + 50),
                              "reason": "audit over-decrement"}, headers=h)
        assert r.status_code == 422, "over-decrement must be rejected"
        # ledger invariant across every movement
        db = SessionLocal()
        try:
            bad = [m.id for m in db.query(models.Movement).all()
                   if (m.qty_before or 0) + (m.qty_change or 0) != (m.qty_after or 0)]
        finally:
            db.close()
        assert not bad, f"movement ledger broken (before+change != after) for ids {bad[:5]}"
        # no negative stock anywhere
        db = SessionLocal()
        try:
            neg = [(s.sku, s.branch, s.qty) for s in db.query(models.Stock).all() if (s.qty or 0) < 0]
        finally:
            db.close()
        assert not neg, f"negative stock rows: {neg[:5]}"


def test_concurrent_stock_decrements_do_not_lose_updates():
    """PHASE 5 race condition: concurrent decrements must not lose updates.
    Regression: read-modify-write let simultaneous adjustments overwrite each
    other, so the ledger recorded -200 units while stock only fell by 25."""
    import threading
    with TestClient(app):
        h = _tok("U-owner")
        client.post("/api/inventory/receive", json={"sku": "MRB-GLD", "branch": "Store A",
                                                    "qty": 200}, headers=h)
        db = SessionLocal()
        try:
            start = int(db.query(models.Stock).filter_by(sku="MRB-GLD", branch="Store A").first().qty)
        finally:
            db.close()
        codes = []

        def worker():
            r = client.post("/api/inventory/adjust",
                            json={"sku": "MRB-GLD", "branch": "Store A", "qty": -5,
                                  "reason": "concurrency"}, headers=h)
            codes.append(r.status_code)

        ts = [threading.Thread(target=worker) for _ in range(30)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        ok = codes.count(200)
        db = SessionLocal()
        try:
            end = int(db.query(models.Stock).filter_by(sku="MRB-GLD", branch="Store A").first().qty)
            bad = [m.id for m in db.query(models.Movement).filter_by(sku="MRB-GLD", branch="Store A").all()
                   if (m.qty_before or 0) + (m.qty_change or 0) != (m.qty_after or 0)]
        finally:
            db.close()
        assert end == start - ok * 5, f"lost updates: start={start} ok={ok} end={end}"
        assert end >= 0 and not bad, "stock went negative or ledger invariant broke under concurrency"


# --------------------------------------------------------- WORKFLOW COMPLETION
def test_approved_transfer_actually_moves_stock():
    """Approving a transfer must move the stock and close the transfer."""
    with TestClient(app):
        h = _tok("U-owner")
        client.post("/api/inventory/receive", json={"sku": "ZYN-CM", "branch": "Store A", "qty": 20}, headers=h)
        prods = client.get("/api/inventory/products?branch=all", headers=h).json()
        p = next(x for x in prods if x["sku"] == "ZYN-CM")
        a0, b0 = p["stock"]["Store A"], p["stock"]["Store B"]
        t = client.post("/api/transfers", json={"sku": "ZYN-CM", "from_branch": "Store A",
                                                "to_branch": "Store B", "qty": 5}, headers=h).json()
        ap = next(a for a in client.get("/api/approvals", headers=h).json() if a["ref"] == t["id"])
        r = client.post(f"/api/approvals/{ap['id']}/approve", json={"comment": "ok"}, headers=h)
        assert r.status_code == 200, r.text
        prods = client.get("/api/inventory/products?branch=all", headers=h).json()
        p2 = next(x for x in prods if x["sku"] == "ZYN-CM")
        assert p2["stock"]["Store A"] == a0 - 5, "source branch stock not reduced"
        assert p2["stock"]["Store B"] == b0 + 5, "destination branch stock not increased"
        tr = next(x for x in client.get("/api/transfers", headers=h).json() if x["id"] == t["id"])
        assert tr["status"] == "approved", f"transfer still {tr['status']}"


def test_approved_purchase_is_marked_approved():
    with TestClient(app):
        h = _tok("U-owner")
        p = client.post("/api/purchases", json={"vendor": "Bulk Co", "branch": "Store A",
                                                "amount": 300}, headers=h).json()
        ap = next(a for a in client.get("/api/approvals", headers=h).json() if a["ref"] == p["id"])
        client.post(f"/api/approvals/{ap['id']}/approve", json={"comment": "ok"}, headers=h)
        row = next(x for x in client.get("/api/purchases?branch=all", headers=h).json() if x["id"] == p["id"])
        assert row["status"] == "approved", f"purchase still {row['status']}"


def test_transfer_requires_sufficient_stock():
    with TestClient(app):
        h = _tok("U-owner")
        r = client.post("/api/transfers", json={"sku": "ZYN-CM", "from_branch": "Store A",
                                                "to_branch": "Store B", "qty": 999999}, headers=h)
        assert r.status_code == 422, "transfer exceeding available stock must be rejected"


# ------------------------------------------------------------------ SECURITY
def test_invalid_and_tampered_jwt_rejected():
    with TestClient(app):
        assert client.get("/api/reports/kpi", headers={"Authorization": "Bearer not-a-token"}).status_code == 401
        good = _tok("U-owner")["Authorization"].split(" ")[1]
        tampered = good[:-3] + ("aaa" if not good.endswith("aaa") else "bbb")
        assert client.get("/api/reports/kpi", headers={"Authorization": "Bearer " + tampered}).status_code == 401
        assert client.get("/api/reports/kpi").status_code == 401


def test_expired_jwt_rejected():
    from jose import jwt as _jwt
    from datetime import datetime, timezone
    from app.config import settings
    with TestClient(app):
        tok = _jwt.encode({"sub": "U-owner", "role": "owner",
                           "exp": datetime.now(timezone.utc) - timedelta(minutes=5)},
                          settings.jwt_secret, algorithm=settings.jwt_alg)
        assert client.get("/api/reports/kpi", headers={"Authorization": "Bearer " + tok}).status_code == 401


def test_role_escalation_via_token_claim_blocked():
    """Role is taken from the database, never from the token claim."""
    from jose import jwt as _jwt
    from datetime import datetime, timezone
    from app.config import settings
    with TestClient(app):
        tok = _jwt.encode({"sub": "U-emp", "role": "owner",
                           "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
                          settings.jwt_secret, algorithm=settings.jwt_alg)
        h = {"Authorization": "Bearer " + tok}
        r = client.get("/api/reports/kpi?period=month&branch=all", headers=h).json()
        assert r.get("can_view_costs") is False and "costs" not in r, "role escalated via token claim"
        assert client.post("/api/inventory/adjust", json={"sku": "RAW-CLS", "branch": "Store A",
                                                          "qty": -1, "reason": "x"}, headers=h).status_code == 403


def test_permission_and_branch_scoping_enforced():
    with TestClient(app):
        emp = _tok("U-emp")
        assert client.post("/api/expenses", json={"branch": "Store A", "category": "Rent",
                                                  "amount": 10}, headers=emp).status_code == 403
        assert client.get("/api/payroll?start=2026-07-01&end=2026-07-31", headers=emp).status_code == 403
        # inventory manager is scoped to Store A only
        inv = _tok("U-inv")
        assert client.post("/api/inventory/receive", json={"sku": "RAW-CLS", "branch": "Store B",
                                                           "qty": 1}, headers=inv).status_code == 403


def test_sql_injection_and_xss_are_inert():
    with TestClient(app):
        h = _tok("U-owner")
        r = client.get("/api/inventory/products?q=' OR 1=1;--", headers=h)
        assert r.status_code == 200 and isinstance(r.json(), list)
        # stored XSS payload must round-trip as data, not be interpreted
        payload = "<script>alert(1)</script>"
        e = client.post("/api/expenses", json={"branch": "Store A", "category": "Other",
                                               "amount": 1, "custom_description": payload}, headers=h)
        assert e.status_code == 201
        assert e.json()["custom_description"] == payload
        # tables still intact after injection attempt
        assert client.get("/api/inventory/products", headers=h).status_code == 200


def test_bot_endpoints_require_bot_token():
    with TestClient(app):
        assert client.post("/api/telegram/auth-token", json={"tg_id": "1"}).status_code == 403
        assert client.get("/api/attendance/approvers?branch=Store A").status_code == 403
