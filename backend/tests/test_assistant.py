"""Business Assistant — intent matching, tools, RBAC, bilingual queries."""
import os, tempfile
_DB = os.path.join(tempfile.gettempdir(), f"smokestack_asst_{os.getpid()}.db")
if os.path.exists(_DB): os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "assistant-secret-long-enough"

from fastapi.testclient import TestClient
from app.main import app
from app.assistant import intent as I, tools as T
from app import permissions as P

import pytest

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _boot():
    """Start the app once: creates tables and seeds demo data."""
    with TestClient(app):
        yield


def _tok(uid="U-owner"):
    r = client.post("/api/auth/login", data={"username": uid, "password": "demo1234"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}

def _ask(q, uid="U-owner", context=None):
    return client.post("/api/assistant/ask", headers=_tok(uid),
                       json={"q": q, "context": context}).json()


# ------------------------------------------------------------ intent matching
def test_english_phrasings_all_reach_sales():
    for q in ["today's sales", "sales today", "how much did we sell today",
              "today's revenue", "show me sales for today"]:
        m = I.classify(q)
        assert m and m["tool"] == "sales.summary", f"{q} -> {m}"
        assert I.extract_period(q) == "today", q

def test_arabic_and_dialect_reach_the_same_tool():
    for q in ["مبيعات اليوم", "قديش بعنا اليوم", "اعرض مبيعات اليوم", "المبيعات اليوم"]:
        m = I.classify(q)
        assert m and m["tool"] == "sales.summary", f"{q} -> {m}"
        assert I.extract_period(q) == "today", q

def test_mixed_language_query():
    m = I.classify("مبيعات today")
    assert m and m["tool"] == "sales.summary"
    assert I.extract_period("مبيعات today") == "today"

def test_arabic_normalisation_handles_variants():
    assert I.normalise("الأرباح") == I.normalise("الارباح")
    assert I.normalise("مبيعــات") == "مبيعات"          # tatweel removed
    assert I.normalise("اليَوْم") == I.normalise("اليوم")  # diacritics removed

def test_profit_and_sales_are_distinguished():
    assert I.classify("today's profit")["intent"] == "profit.summary"
    assert I.classify("today's sales")["intent"] == "sales.summary"
    assert I.classify("ربح اليوم")["intent"] == "profit.summary"

def test_period_extraction_prefers_the_longest_phrase():
    assert I.extract_period("sales last week") == "last_week"
    assert I.extract_period("sales this week") == "week"
    assert I.extract_period("المبيعات الشهر الماضي") == "last_month"
    assert I.extract_period("مبيعات الشهر") == "month"

def test_intents_cover_every_documented_example():
    cases = {
        "low stock": "inventory.low_stock",
        "which products are about to run out": "inventory.low_stock",
        "compare branches": "sales.by_branch",
        "which branch made the most revenue": "sales.by_branch",
        "payroll cost this month": "payroll.summary",
        "who arrived late today": "employees.attendance",
        "show unpaid invoices": "customers.outstanding",
        "best selling products": "products.best_sellers",
        "inventory value": "inventory.value",
        "expenses by category": "expenses.by_category",
        "licenses expiring": "licenses.status",
        "pending approvals": "approvals.pending",
    }
    for q, expected in cases.items():
        m = I.classify(q)
        assert m and m["tool"].startswith(expected.split(".")[0]), f"{q} -> {m}"
        assert m["intent"] == expected or m["tool"] == expected, f"{q} -> {m}"


# ---------------------------------------------------------------- navigation
def test_navigation_english_and_arabic():
    assert I.detect_navigation("go to purchases") == "purchases"
    assert I.detect_navigation("open telegram") == "telegram"
    assert I.detect_navigation("افتح المشتريات") == "purchases"
    assert I.detect_navigation("open the dashboard") == "dash"

def test_navigation_does_not_hijack_questions():
    assert I.detect_navigation("how much did we sell today") is None
    out = _ask("today's sales")
    assert out["kind"] == "answer"

def test_navigate_returns_a_view():
    out = _ask("go to purchases")
    assert out["kind"] == "navigate" and out["view"] == "purchases"


# --------------------------------------------------------------- calculations
def test_sales_matches_the_dashboard_engine():
    from app.database import SessionLocal
    from app.routers import core as C
    from app import reports_tg as R
    out = _ask("today's sales")
    db = SessionLocal()
    try:
        today = R.business_date(db)
        cp = C._costs_profit(db, ["Store A", "Store B", "Store C"], today, today)
        assert out["data"]["sales"] == round(float(cp["revenue"]), 2)
        assert out["data"]["profit"] == round(float(cp["profit"]), 2)
    finally:
        db.close()

def test_every_number_is_explainable():
    out = _ask("today's sales")
    ex = out["explain"]
    assert ex and ex["steps"] and ex["engine"]
    assert any("Profit = sales" in s for s in ex["steps"])
    assert "core._costs_profit" in ex["engine"]

def test_branch_comparison_ranks_and_names_best_and_weakest():
    out = _ask("compare branches this month")
    d = out["data"]
    assert d["rows"] and d["best"] and d["weakest"]
    sales = [r["sales"] for r in d["rows"]]
    assert sales == sorted(sales, reverse=True), "rows must be ranked"

def test_low_stock_returns_actionable_rows():
    out = _ask("low stock")
    assert "counts" in out["data"]
    assert isinstance(out["data"]["low_stock"], list)
    assert out["answer"]


# ------------------------------------------------------------ business rules
def test_rules_fire_and_carry_a_next_action():
    out = _ask("low stock")
    assert isinstance(out["warnings"], list)
    for w in out["warnings"]:
        assert w["level"] in ("critical", "warning", "ok") and w["text"]

def test_attendance_rules_flag_absences():
    out = _ask("attendance today")
    assert "counts" in out["data"]
    assert isinstance(out["warnings"], list)


# ------------------------------------------------------------------- security
def test_cashier_cannot_reach_payroll_through_the_assistant():
    out = _ask("payroll this month", uid="U-cash")
    assert out["ok"] is False and out["kind"] == "denied"
    assert "permission" in out["answer"].lower()

def test_employee_cannot_see_profit_or_costs():
    out = _ask("today's sales", uid="U-emp")
    assert out["ok"] is True
    assert out["data"].get("profit") is None
    assert out["data"].get("costs") is None
    assert set(out["hidden_by_permission"]) >= {"costs", "profit"}

def test_asking_for_profit_without_permission_is_denied_not_faked():
    out = _ask("profit today", uid="U-emp")
    assert out["ok"] is False and out["kind"] == "denied"

def test_tool_registry_enforces_permission_itself():
    from app.database import SessionLocal
    from app import models
    db = SessionLocal()
    try:
        cash = db.get(models.User, "U-cash")
        try:
            T.run("payroll.summary", db, cash)
            assert False, "the registry must refuse this"
        except T.Denied:
            pass
    finally:
        db.close()

def test_tool_list_is_filtered_per_user():
    owner = client.get("/api/assistant/tools", headers=_tok()).json()
    cash = client.get("/api/assistant/tools", headers=_tok("U-cash")).json()
    assert owner["total_registered"] == len(T.REGISTRY)
    assert len(cash["tools"]) < len(owner["tools"])
    assert not any(t["name"] == "payroll.summary" for t in cash["tools"])

def test_branch_scope_is_enforced():
    out = _ask("sales for Store B", uid="U-cash")   # cashier is Store A only
    assert out["ok"] is False and out["kind"] == "denied"

def test_run_endpoint_returns_403_not_a_silent_empty_result():
    r = client.post("/api/assistant/run", headers=_tok("U-cash"),
                    json={"tool": "payroll.summary", "args": {}})
    assert r.status_code == 403

def test_every_tool_declares_an_existing_permission():
    for name, t in T.REGISTRY.items():
        assert t["perm"] in P.ALL_PERMS, f"{name} uses an unknown permission {t['perm']}"


# ---------------------------------------------------------------- conversation
def test_follow_up_reuses_the_previous_tool():
    first = _ask("today's sales")
    second = _ask("now yesterday", context=first["context"])
    assert second["ok"] and second["tool"] == "sales.summary"
    assert second["data"]["period"] == "yesterday"

def test_unknown_query_offers_help_rather_than_guessing():
    out = _ask("qwertyuiop zxcvbnm")
    assert out["ok"] is False and out["kind"] == "unknown"
    assert out["suggestions"]


# ---------------------------------------------------------------------- search
def test_product_search_finds_a_seeded_product():
    out = _ask("find product Marlboro")
    assert out["ok"] and out["tool"] == "inventory.search"
    assert out["data"]["count"] >= 1
    assert any("Marlboro" in r["name"] for r in out["data"]["results"])

def test_search_hides_cost_from_users_without_view_cost():
    out = _ask("find product Marlboro", uid="U-emp")
    assert out["ok"]
    assert all(r["cost"] is None for r in out["data"]["results"])

def test_parse_endpoint_exposes_the_decision():
    r = client.get("/api/assistant/parse?q=today's%20sales", headers=_tok()).json()
    assert r["intent"]["tool"] == "sales.summary"
    assert r["period"] == "today"

def test_assistant_requires_authentication():
    assert client.post("/api/assistant/ask", json={"q": "sales"}).status_code == 401
    assert client.get("/api/assistant/tools").status_code == 401
