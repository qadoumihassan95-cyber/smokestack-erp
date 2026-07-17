"""Worker unit tests: menus, routing, aggregation, permission gates, pagination,
report export, and the Phase-2 operational flows (expense/receive/adjust/transfer/
purchase) — all with the API stubbed."""
import os
import asyncio
from datetime import date, timedelta

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")
import worker as W


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _btn_texts(m):
    return [b.text for row in m.inline_keyboard for b in row]


def _btn_cbs(m):
    return [b.callback_data for row in m.inline_keyboard for b in row]


# ---------- menus / keyboards ----------
def test_home_menu_is_operations_first():
    txt = _btn_texts(W.home_kb())
    for label in ["💸 Add Expense", "📦 Inventory", "📥 Receive Stock", "🔄 Transfer Stock",
                  "🧮 Adjust Stock", "🛒 Purchases", "🔍 Search Product", "📸 Scan Barcode",
                  "📊 Reports & Insights", "⚙️ Settings"]:
        assert label in txt, label


def test_reports_submenu_has_reporting_buttons():
    txt = _btn_texts(W.reports_home_kb())
    for label in ["📊 Sales", "💰 Profit", "💸 Expenses", "📑 Reports", "🔔 Notifications"]:
        assert label in txt, label


def test_callback_data_within_limits():
    for m in (W.home_kb(), W.reports_home_kb(), W.unlinked_kb()):
        for cb in _btn_cbs(m):
            assert len(cb.encode()) <= 64, cb


# ---------- pure helpers ----------
def test_period_range_and_agg():
    t = date.today()
    assert W.period_range("month")[0] == t.replace(day=1)
    rows = [{"branch": "A", "amount": 100, "tax": 8, "date": t.isoformat()},
            {"branch": "B", "amount": 30, "tax": 2, "date": t.isoformat()}]
    a = W.agg_sales(W.in_range(rows, t, t))
    assert a["total"] == 130 and a["tx"] == 2


def test_validate():
    assert W._validate("amount", "250")[0] == 250.0
    assert W._validate("amount", "-5")[1]                      # negative rejected
    assert W._validate("amount", "abc")[1]                     # non-numeric rejected
    assert W._validate("qty", "0")[1]                          # non-positive rejected
    assert W._validate("qty", "12")[0] == 12
    assert W._validate("text", "")[1]                          # empty rejected


def test_permission_map():
    assert W.has("owner", "adjust_stock") and W.has("owner", "create")
    assert W.has("cashier", "create") and not W.has("cashier", "adjust_stock")
    assert not W.has("employee", "create")


def test_report_csv():
    b = W.report_csv("Sales", [["Total", "$100"]])
    assert b"Sales" in b and b"Total" in b


# ---------- API-stubbed dispatch + flows ----------
class Fake:
    def __init__(self, role="owner", cost=True):
        self.role = role
        self.cost = cost
        self.calls = []

    async def get_ctx(self, tg_id):
        return "tkn", {"id": "U-x", "name": "Owner", "role": self.role, "branches": None}, {}

    async def req(self, method, path, token=None, body=None, headers=None):
        self.calls.append((method, path, body))
        if path.startswith("/api/branches"):
            return 200, ["Store A", "Store B", "Store C"]
        if path.startswith("/api/inventory/products"):
            return 200, [{"sku": "RAW-CLS", "name": "RAW Classic", "total": 5, "min": 40, "price": 2.5,
                          "cost": 0.9, "supplier": "HBI", "barcode": "x", "stock": {"Store A": 5}}]
        if path.startswith("/api/reports/dashboard"):
            d = {"sales_today": 18300, "expenses_today": 320, "profit_today": 16000,
                 "inventory_units": 434, "low": 1, "out": 2}
            if self.cost:
                d["inventory_cost"] = 1894
            return 200, d
        if path.startswith("/api/sales"):
            return 200, [{"branch": "Store A", "amount": 8420, "tax": 695, "date": date.today().isoformat()}]
        if method == "POST" and path == "/api/expenses":
            return 201, {"id": "L-77", "branch": body["branch"]}
        if method == "POST" and path == "/api/inventory/receive":
            return 200, {"new_stock": 5 + int(body["qty"])}
        if method == "POST" and path == "/api/inventory/adjust":
            return 200, {"new_stock": 5 + int(body["qty"])}
        if method == "POST" and path == "/api/transfers":
            return 201, {"id": "TR-1", "status": "pending"}
        if method == "POST" and path == "/api/purchases":
            return 201, {"id": "PO-1", "status": "pending_approval"}
        if path == "/api/telegram/audit":
            return 200, {"ok": True}
        return 200, {}


def _patch(fake):
    W.get_ctx = fake.get_ctx
    W._req = fake.req


def test_start_expense_shows_branch_step():
    _patch(Fake(role="owner"))
    text, markup, _ = run(W.start_flow("e1", "exp"))
    assert "Add Expense" in text and "branch" in text.lower()
    assert any(cb.startswith("f:pick:") for cb in _btn_cbs(markup))


def test_expense_denied_for_unauthorized_role():
    _patch(Fake(role="employee"))
    text, _, _ = run(W.start_flow("e2", "exp"))
    assert "permission" in text.lower()


def test_expense_full_flow_persists():
    f = Fake(role="owner")
    _patch(f)
    tg = "e3"
    run(W.start_flow(tg, "exp"))
    run(W.dispatch(tg, "f:pick:0"))     # branch Store A
    run(W.dispatch(tg, "f:pick:1"))     # category Utilities
    run(W.flow_text(tg, "250"))         # amount
    run(W.dispatch(tg, "f:pick:0"))     # payment Cash
    run(W.dispatch(tg, "f:skip"))       # notes skip
    text, markup, _ = run(W.dispatch(tg, "f:skip"))   # receipt skip -> confirm
    assert "confirm" in text.lower() and "f:go" in _btn_cbs(markup)
    text2, _, _ = run(W.dispatch(tg, "f:go"))
    assert "Expense saved" in text2 and "L-77" in text2
    assert any(m == "POST" and p == "/api/expenses" for (m, p, b) in f.calls)


def test_expense_invalid_amount():
    _patch(Fake(role="owner"))
    tg = "e4"
    run(W.start_flow(tg, "exp"))
    run(W.dispatch(tg, "f:pick:0"))
    run(W.dispatch(tg, "f:pick:1"))
    assert "number" in run(W.flow_text(tg, "abc"))[0].lower()


def test_receive_flow_updates_stock():
    f = Fake(role="inventory_manager")
    _patch(f)
    tg = "r1"
    run(W.start_flow(tg, "recv"))
    run(W.flow_pick_product(tg, "RAW-CLS"))   # product
    run(W.dispatch(tg, "f:pick:0"))           # branch
    run(W.flow_text(tg, "12"))                # qty
    run(W.dispatch(tg, "f:skip"))             # supplier
    run(W.dispatch(tg, "f:skip"))             # unit cost
    run(W.dispatch(tg, "f:skip"))             # invoice
    run(W.dispatch(tg, "f:skip"))             # file -> confirm
    text, _, _ = run(W.dispatch(tg, "f:go"))
    assert "received" in text.lower() and "17" in text
    assert any(m == "POST" and p == "/api/inventory/receive" for (m, p, b) in f.calls)


def test_adjust_set_exact_computes_delta():
    f = Fake(role="owner")
    _patch(f)
    tg = "a1"
    run(W.start_flow(tg, "adj"))
    run(W.flow_pick_product(tg, "RAW-CLS"))   # product (current qty 5 @ Store A)
    run(W.dispatch(tg, "f:pick:0"))           # branch Store A
    run(W.dispatch(tg, "f:pick:2"))           # type = set exact
    run(W.flow_text(tg, "10"))                # target 10
    run(W.dispatch(tg, "f:pick:0"))           # reason
    run(W.dispatch(tg, "f:skip"))             # notes -> confirm
    text, _, _ = run(W.dispatch(tg, "f:go"))
    assert "adjusted" in text.lower()
    body = next(b for (m, p, b) in f.calls if p == "/api/inventory/adjust")
    assert body["qty"] == 5                    # delta = target(10) - current(5)


def test_transfer_insufficient_stock_blocked():
    _patch(Fake(role="owner"))
    tg = "x1"
    run(W.start_flow(tg, "xfer"))
    run(W.flow_pick_product(tg, "RAW-CLS"))   # 5 available @ Store A
    run(W.dispatch(tg, "f:pick:0"))           # from Store A
    run(W.dispatch(tg, "f:pick:0"))           # to (excludes A) -> Store B
    run(W.flow_text(tg, "9999"))              # qty
    run(W.dispatch(tg, "f:skip"))             # notes -> confirm
    text, _, _ = run(W.dispatch(tg, "f:go"))
    assert "available" in text.lower()


def test_transfer_success_submits_pending():
    f = Fake(role="owner")
    _patch(f)
    tg = "x2"
    run(W.start_flow(tg, "xfer"))
    run(W.flow_pick_product(tg, "RAW-CLS"))
    run(W.dispatch(tg, "f:pick:0"))
    run(W.dispatch(tg, "f:pick:0"))
    run(W.flow_text(tg, "3"))
    run(W.dispatch(tg, "f:skip"))
    text, _, _ = run(W.dispatch(tg, "f:go"))
    assert "submitted" in text.lower() and "pending" in text.lower()
    assert any(p == "/api/transfers" for (m, p, b) in f.calls)


def test_purchase_flow():
    f = Fake(role="owner")
    _patch(f)
    tg = "p1"
    run(W.start_flow(tg, "pur"))
    run(W.dispatch(tg, "f:pick:0"))     # branch
    run(W.flow_text(tg, "ACME Supply")) # vendor
    run(W.flow_text(tg, "500"))         # amount
    run(W.dispatch(tg, "f:skip"))       # invoice -> confirm
    text, _, _ = run(W.dispatch(tg, "f:go"))
    assert "Purchase submitted" in text and "PO-1" in text


def test_cancel_ends_flow():
    _patch(Fake(role="owner"))
    tg = "c1"
    run(W.start_flow(tg, "exp"))
    text, _, _ = run(W.dispatch(tg, "f:cancel"))
    assert "cancel" in text.lower()
    assert W.flow(tg) is None


def test_confirm_dedup_guard():
    f = Fake(role="owner")
    _patch(f)
    tg = "d1"
    run(W.start_flow(tg, "exp"))
    run(W.dispatch(tg, "f:pick:0")); run(W.dispatch(tg, "f:pick:1"))
    run(W.flow_text(tg, "10")); run(W.dispatch(tg, "f:pick:0"))
    run(W.dispatch(tg, "f:skip")); run(W.dispatch(tg, "f:skip"))
    fl = W.flow(tg)
    fl["submitting"] = True                    # simulate an in-flight submit
    text, _, _ = run(W.flow_submit(tg))
    assert "processing" in text.lower()


# ---------- Phase-1: attendance ----------
class AttFake:
    """API stub for attendance flows. `clockin` controls the clock-in response shape."""
    def __init__(self, role="employee", consent=True, clockin=None):
        self.role = role
        self.consent = consent
        self.clockin = clockin or {"result": "in", "status": "active", "branch": "Store A",
                                   "distance": 42, "time": "2026-07-17T09:00:00+00:00", "late": False}
        self.calls = []

    async def get_ctx(self, tg_id):
        return "tkn", {"id": "U-emp", "name": "Sam Rivera", "role": self.role, "branches": ["Store A"]}, \
               {"att_consent": self.consent}

    async def req(self, method, path, token=None, body=None, headers=None):
        self.calls.append((method, path, body))
        if path == "/api/attendance/clock-in":
            return 200, self.clockin
        if path == "/api/attendance/clock-out":
            return 200, {"result": "out", "branch": "Store A", "clock_in": "2026-07-17T09:00:00+00:00",
                         "clock_out": "2026-07-17T17:30:00+00:00", "worked_minutes": 510}
        if path == "/api/attendance/today":
            return 200, {"state": "active", "branch": "Store A", "clock_in": "2026-07-17T09:00:00+00:00",
                         "ci_dist": 42}
        if path.startswith("/api/attendance/me"):
            return 200, [{"clock_in": "2026-07-17T09:00:00+00:00", "branch": "Store A",
                          "status": "completed", "worked_minutes": 510}]
        if path == "/api/attendance/pending":
            return 200, [{"id": 7, "employee": "Ana Gomez", "branch": "Store A", "ci_dist": 300,
                          "reason": "Delivery", "approval": "pending"}]
        if path.startswith("/api/attendance/7/approve"):
            return 200, {"status": "active", "employee": "Ana Gomez", "tg_id": None, "branch": "Store A"}
        if path.startswith("/api/attendance/branch/"):
            return 200, {"radius_m": 150, "lat": 32.2211, "loc_verify": True}
        if path.startswith("/api/branches"):
            return 200, ["Store A"]
        if path.startswith("/api/telegram/prefs"):
            return 200, {"prefs": {"att_consent": True}}
        return 200, {}


def _att_patch(fake):
    W.get_ctx = fake.get_ctx
    W._req = fake.req


def test_att_menu_employee_no_approvals():
    _att_patch(AttFake(role="employee"))
    text, markup, _ = run(W.render_att_menu("m1"))
    labels = _btn_texts(markup)
    assert any("Clock In" in l for l in labels) and any("Clock Out" in l for l in labels)
    assert not any("Approval" in l for l in labels)      # employees don't see approvals


def test_att_menu_manager_sees_approvals():
    _att_patch(AttFake(role="branch_manager"))
    _, markup, _ = run(W.render_att_menu("m2"))
    assert any("Approval" in l for l in _btn_texts(markup))


def test_att_consent_gate_prompts_first():
    _att_patch(AttFake(consent=False))

    class Upd:
        effective_chat = type("C", (), {"id": 111})()
    sent = {}

    async def fake_show(update, ctx, text, markup):
        sent["text"] = text; sent["markup"] = markup
    orig = W.show
    W.show = fake_show
    try:
        run(W.handle_att_location_cb(Upd(), None, "m3", "att:in"))
    finally:
        W.show = orig
    assert "privacy" in sent["text"].lower()
    assert any(cb.startswith("att:consent:") for cb in _btn_cbs(sent["markup"]))


def test_att_clock_in_inside_radius_message():
    f = AttFake()
    _att_patch(f)
    text, _, _ = run(W.attendance_submit("m4", "in", 32.2211, 35.2544, True))
    assert "Clock-in successful" in text and "42 meters" in text
    assert any(p == "/api/attendance/clock-in" for (m, p, b) in f.calls)


def test_att_clock_in_outside_creates_pending():
    f = AttFake(clockin={"result": "pending", "branch": "Store A", "distance": 300,
                         "radius": 150, "id": 9})
    _att_patch(f)
    text, _, _ = run(W.attendance_submit("m5", "in", 32.30, 35.25, True))
    assert "approval" in text.lower() and "300 m" in text


def test_att_clock_in_choose_branch():
    f = AttFake(clockin={"result": "choose",
                         "candidates": [{"branch": "Store A", "distance": 40},
                                        {"branch": "Store B", "distance": 80}]})
    _att_patch(f)
    text, markup, _ = run(W.attendance_submit("m6", "in", 32.2211, 35.2544, True))
    assert "choose" in text.lower()
    assert any(cb.startswith("att:choose:") for cb in _btn_cbs(markup))


def test_att_clock_out_shows_duration():
    _att_patch(AttFake())
    text, _, _ = run(W.attendance_submit("m7", "out", 32.2211, 35.2544, True))
    assert "Clock-out successful" in text and "8h 30m" in text


def test_att_today_status_render():
    _att_patch(AttFake())
    text, _, _ = run(W.render_att_today("m8"))
    assert "clocked in" in text.lower() and "Store A" in text


def test_att_pending_render_has_decision_buttons():
    _att_patch(AttFake(role="owner"))
    text, markup, _ = run(W.render_att_pending("m9"))
    cbs = _btn_cbs(markup)
    assert any(c.startswith("att:appr:7") for c in cbs) and any(c.startswith("att:rej:7") for c in cbs)


def test_att_pending_blocked_for_employee():
    _att_patch(AttFake(role="employee"))
    text, _, _ = run(W.render_att_pending("m10"))
    assert "permitted" in text.lower()
