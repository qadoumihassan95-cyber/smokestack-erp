"""SEC HIGH-08 regression — `/api/inventory/asof` must not disclose profit to roles
that lack `view_profit`.

At `3eb1ccd` the endpoint was gated by `view_asof` and then returned its whole payload
raw, including a per-row `profit` and a total `profit`. Three roles hold `view_asof`
WITHOUT `view_profit` — `branch_manager`, `manager`, `inventory_manager` — so each read
margin here that every other surface refuses them.

The two permissions answer different questions. `view_asof` is "may you open this
report"; `view_profit` is "may you see this number in it". A route-level permission
passing is not a reason to stop applying field-level protection on a financial read.

METHOD. The auditor found this with a value-keyed matrix: distinctive sentinel figures
planted in the database, every GET called as every role, responses flattened to numeric
leaves — so a renamed or newly added key cannot evade it. This module keeps that shape
rather than asserting `"profit" not in body`, which any rename would satisfy. The
sentinel is planted through the real product-creation path, and test 0 proves the
sentinel is actually reachable before any refusal is claimed to mean something.

NOT CLOSED BY THIS. `cost` remains visible to these three roles and that is correct —
they all hold `view_cost`. Test 3 pins it, so a later over-broad "redact everything
financial" would fail here rather than quietly breaking the inventory manager's job.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"sec_h08_asof_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("SEED_ON_START", "true")
os.environ.setdefault("SEED_DEMO_DATA", "true")
os.environ.setdefault("JWT_SECRET", "sec-h08-secret-long-enough-for-tests")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")

from datetime import date                            # noqa: E402

from fastapi.testclient import TestClient            # noqa: E402

from app.main import app                             # noqa: E402
from app import models, permissions as P, tenancy    # noqa: E402

with TestClient(app):
    pass
client = TestClient(app)

PW = "demo1234"
TODAY = date.today().isoformat()

# Distinctive figures nothing else in the fixture produces, so a match in a response
# is this SKU's margin and not an arithmetic coincidence.
SENTINEL_SKU = "SEC-H08-SKU"
SENTINEL_QTY = 7
SENTINEL_COST = 111.0        # 7 x 111 = 777   cost
SENTINEL_PRICE = 333.0       # 7 x 333 = 2331  retail  ->  1554  profit
SENTINEL_COST_VALUE = SENTINEL_QTY * SENTINEL_COST
SENTINEL_PROFIT = SENTINEL_QTY * (SENTINEL_PRICE - SENTINEL_COST)

# view_asof AND view_cost, but NOT view_profit. The exact gap the finding names.
UNENTITLED = ["U-bm", "U-inv"]          # branch_manager, inventory_manager
ENTITLED = "U-owner"
BRANCH = "Store A"


def _h(uid, pw=PW):
    r = client.post("/api/auth/login", data={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _numbers(o, out=None):
    out = [] if out is None else out
    if isinstance(o, dict):
        for v in o.values():
            _numbers(v, out)
    elif isinstance(o, list):
        for v in o:
            _numbers(v, out)
    elif isinstance(o, (int, float)) and not isinstance(o, bool):
        out.append(float(o))
    return out


def _keys(o, out=None):
    out = set() if out is None else out
    if isinstance(o, dict):
        for k, v in o.items():
            out.add(k)
            _keys(v, out)
    elif isinstance(o, list):
        for v in o:
            _keys(v, out)
    return out


def setup_module(_m):
    """Plant the sentinel through the real creation path, then set the stock
    directly so the as-of figure is exact and independent of movement history."""
    r = client.post("/api/inventory/products",
                    json={"sku": SENTINEL_SKU, "name": "Sentinel Item",
                          "cost": SENTINEL_COST, "price": SENTINEL_PRICE,
                          "min_level": 1, "supplier": "Philip Morris"},
                    headers=_h(ENTITLED))
    assert r.status_code in (200, 201), f"could not plant the sentinel: {r.text[:300]}"
    with tenancy.tenant_session(1) as db:
        st = (db.query(models.Stock)
              .filter(models.Stock.sku == SENTINEL_SKU, models.Stock.branch == BRANCH).first())
        assert st is not None, "product creation did not create a stock row"
        st.qty = SENTINEL_QTY
        db.commit()


def teardown_module(_m):
    with tenancy.tenant_session(1) as db:
        db.query(models.Stock).filter(models.Stock.sku == SENTINEL_SKU).delete()
        db.query(models.Movement).filter(models.Movement.sku == SENTINEL_SKU).delete()
        db.query(models.Product).filter(models.Product.sku == SENTINEL_SKU).delete()
        db.commit()


# ===========================================================================
# 0. CONTROLS.
# ===========================================================================
def test_0_the_sentinel_profit_is_actually_reachable_on_this_endpoint():
    """Without this, 'the number is absent' proves nothing — it could be absent
    because the report never contained it."""
    r = client.get(f"/api/inventory/asof?date={TODAY}&branch={BRANCH}", headers=_h(ENTITLED))
    assert r.status_code == 200, r.text
    nums = _numbers(r.json())
    assert SENTINEL_PROFIT in nums, (
        f"the entitled role does not see profit {SENTINEL_PROFIT} on this endpoint; "
        f"the refusals below would be vacuous")
    assert "profit" in _keys(r.json())


def test_1_the_role_matrix_is_what_the_finding_assumes():
    for role in ("branch_manager", "manager", "inventory_manager"):
        assert P.can(role, "view_asof"), f"{role} can no longer reach the endpoint"
        assert not P.can(role, "view_profit"), (
            f"{role} now holds view_profit — this module no longer tests a "
            f"disclosure to an unentitled role")
        assert P.can(role, "view_cost"), (
            f"{role} lost view_cost; test 3 below would then pass for the wrong reason")


# ===========================================================================
# 1. THE FINDING.
# ===========================================================================
def test_2_asof_does_not_disclose_profit_to_roles_lacking_view_profit():
    for uid in UNENTITLED:
        r = client.get(f"/api/inventory/asof?date={TODAY}&branch={BRANCH}", headers=_h(uid))
        assert r.status_code == 200, f"{uid}: {r.text[:200]}"
        body = r.json()

        assert "profit" not in _keys(body), (
            f"{uid} received a `profit` key on /api/inventory/asof")
        assert SENTINEL_PROFIT not in _numbers(body), (
            f"{uid} received the margin value {SENTINEL_PROFIT} on /api/inventory/asof "
            f"under some other key — the disclosure survived a rename")


def test_3_the_same_roles_still_receive_cost_which_they_are_entitled_to():
    """The fix is field-level and specific. Over-redacting would break the
    inventory manager's actual job, and would look identical to a correct fix in
    a test that only asserted profit is absent."""
    for uid in UNENTITLED:
        r = client.get(f"/api/inventory/asof?date={TODAY}&branch={BRANCH}", headers=_h(uid))
        assert r.status_code == 200, r.text
        nums = _numbers(r.json())
        assert SENTINEL_COST_VALUE in nums, (
            f"{uid} holds view_cost but no longer receives the cost valuation "
            f"{SENTINEL_COST_VALUE} — the redaction is too broad")


def test_4_a_role_lacking_view_cost_receives_neither_cost_nor_profit():
    """`cost_value` — the report's total valuation — was missing from the
    name-keyed map entirely. An AGGREGATE of a protected field is protected; it
    was simply never spelled, which is the standing failure mode of a denylist
    keyed on field names.
    """
    if not P.can("cashier", "view_asof"):
        r = client.get(f"/api/inventory/asof?date={TODAY}&branch={BRANCH}", headers=_h("U-cash"))
        assert r.status_code == 403, (
            f"cashier lacks view_asof but got {r.status_code}")
        return
    r = client.get(f"/api/inventory/asof?date={TODAY}&branch={BRANCH}", headers=_h("U-cash"))
    assert r.status_code == 200, r.text
    nums = _numbers(r.json())
    assert SENTINEL_COST_VALUE not in nums, "a role without view_cost received cost_value"
    assert SENTINEL_PROFIT not in nums, "a role without view_profit received profit"
