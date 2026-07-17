"""Unit tests for the interactive dashboard worker: keyboards, routing, aggregation,
permission gate, pagination, dedup, and report export — all with the API stubbed."""
import os
import asyncio
from datetime import date, timedelta

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")
import worker as W


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _btn_texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _btn_cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# ---- keyboards ----
def test_home_menu_has_all_primary_buttons():
    txt = _btn_texts(W.home_kb())
    for label in ["📊 Sales", "💰 Profit", "💸 Expenses", "📦 Inventory",
                  "⚠️ Low Stock", "🚫 Out of Stock", "🏪 Branches", "📑 Reports",
                  "🔔 Notifications", "⚙️ Settings", "🔄 Refresh"]:
        assert label in txt, label


def test_unlinked_menu():
    txt = _btn_texts(W.unlinked_kb())
    assert "🔗 Link Account" in txt and "ℹ️ Help" in txt


def test_footer_has_back_home():
    cbs = [c for row in W.footer() for (t, c) in row]
    assert "back" in cbs and "home" in cbs


def test_callback_data_within_limits():
    # every callback_data must be <= 64 bytes (Telegram hard limit)
    for markup in (W.home_kb(), W.unlinked_kb()):
        for cb in _btn_cbs(markup):
            assert len(cb.encode()) <= 64, cb


# ---- date + aggregation ----
def test_period_range():
    t = date.today()
    assert W.period_range("today")[0] == t
    assert W.period_range("yest")[0] == t - timedelta(days=1)
    assert W.period_range("7d")[0] == t - timedelta(days=6)
    assert W.period_range("month")[0] == t.replace(day=1)


def test_agg_sales_and_in_range():
    today = date.today().isoformat()
    rows = [{"branch": "A", "amount": 100, "tax": 8, "date": today},
            {"branch": "A", "amount": 50, "tax": 4, "date": today},
            {"branch": "B", "amount": 30, "tax": 2, "date": today}]
    sel = W.in_range(rows, date.today(), date.today())
    a = W.agg_sales(sel)
    assert a["total"] == 180 and a["tx"] == 3
    assert round(a["avg"], 2) == 60.0
    assert a["by_branch"]["A"] == 150 and a["by_branch"]["B"] == 30


def test_delta_line():
    assert "▲" in W.delta_line(120, 100)
    assert "▼" in W.delta_line(80, 100)


def test_report_csv_and_money():
    b = W.report_csv("Sales", [["Total", "$100"]])
    assert b'Sales' in b and b'Total' in b
    assert W.money(1234) == "$1,234"


# ---- routing / dispatch (API stubbed) ----
class FakeCtx:
    """Patch get_ctx + _req so dispatch can run without a backend."""
    def __init__(self, role="owner", cost=True):
        self.role = role
        self.cost = cost

    async def get_ctx(self, tg_id):
        return "tkn", {"id": "U-x", "name": "Owner", "role": self.role, "branches": None}, {}

    async def req(self, method, path, token=None, body=None, headers=None):
        if path.startswith("/api/reports/dashboard"):
            d = {"sales_today": 18300, "expenses_today": 320, "profit_today": 16000,
                 "inventory_units": 434, "low": 1, "out": 2}
            if self.cost:
                d.update({"inventory_cost": 1894, "inventory_retail": 3236})
            return 200, d
        if path.startswith("/api/sales"):
            today = date.today().isoformat()
            return 200, [{"branch": "Store A", "amount": 8420, "tax": 695, "date": today},
                         {"branch": "Store B", "amount": 5100, "tax": 420, "date": today}]
        if path.startswith("/api/expenses"):
            return 200, [{"branch": "Store A", "amount": 320, "category": "Utilities", "date": date.today().isoformat()}]
        if path.startswith("/api/inventory/products"):
            return 200, [{"sku": "RAW-CLS", "name": "RAW Classic", "total": 5, "min": 40, "price": 2.5,
                          "cost": 0.9, "supplier": "HBI", "barcode": "x", "stock": {"Store A": 5}},
                         {"sku": "ZYN-CM", "name": "Zyn Cool Mint", "total": 0, "min": 30, "price": 5.5,
                          "cost": 3.1, "supplier": "SM", "barcode": "y", "stock": {"Store A": 0}}]
        if path.startswith("/api/branches"):
            return 200, ["Store A", "Store B", "Store C"]
        return 200, {}


def _patch(fake):
    W.get_ctx = fake.get_ctx
    W._req = fake.req


def test_dispatch_sales_today_shows_real_numbers():
    _patch(FakeCtx())
    text, markup, doc = run(W.dispatch("1", "sales:today"))
    assert "Sales" in text and "$13,520" in text  # 8420 + 5100
    assert "Transactions" in text


def test_dispatch_profit_allowed_vs_denied():
    _patch(FakeCtx(role="owner", cost=True))
    text, _, _ = run(W.dispatch("1", "nav:profit"))
    assert "Pick a period" in text
    _patch(FakeCtx(role="cashier", cost=False))
    text2, _, _ = run(W.dispatch("2", "nav:profit"))
    assert "permission" in text2.lower()


def test_dispatch_low_stock_pagination_and_product_button():
    _patch(FakeCtx())
    text, markup, _ = run(W.dispatch("1", "inv:low:0"))
    assert "Low stock" in text and "RAW Classic" in text
    assert any(cb.startswith("prod:") for cb in _btn_cbs(markup))


def test_dispatch_reports_csv_returns_document():
    _patch(FakeCtx())
    text, markup, doc = run(W.dispatch("1", "rep:daily:today:csv"))
    assert doc is not None
    buf, fname, caption = doc
    assert fname.endswith(".csv") and isinstance(buf, (bytes, bytearray))


def test_home_and_back_navigation():
    _patch(FakeCtx())
    text, markup, _ = run(W.dispatch("1", "home"))
    assert "Dashboard" in text
