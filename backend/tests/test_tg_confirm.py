"""Telegram confirmation flow — success card + auto-return + error card, and
'never leave a duplicate message'. Exercises the real worker handlers with the
network + bot mocked, so it verifies the UX contract without touching Telegram.
"""
import os, sys, asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "telegram_worker"))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")
os.environ.setdefault("SMOKESTACK_API_BASE", "http://x")

import worker  # noqa: E402


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------- mocks
def _mock(exp_status=200, exp_data=None):
    """Patch the network + auth + audit + capabilities used by the flow."""
    exp_data = exp_data if exp_data is not None else {"id": 12345, "new_stock": 7, "status": "pending"}

    async def fake_req(method, path, token=None, body=None, headers=None):
        if "/api/expenses" in path or "/api/inventory" in path or "/api/transfers" in path \
                or "/api/purchases" in path:
            return (exp_status, exp_data)
        if "capabilities" in path:
            return (200, {"capabilities": {"expenses": True, "inventory": True,
                                           "transfer": True, "purchases": True}})
        if "auth-token" in path:
            return (200, {"access_token": "t", "user": {"id": "U-owner", "name": "Owner"}})
        return (200, {})

    async def fake_ctx(tg_id):
        return ("tok", {"id": "U-owner", "name": "Owner"}, {})

    async def fake_audit(*a, **k):
        return None

    async def fake_qty(token, sku, branch):
        return 5

    worker._req = fake_req
    worker.get_ctx = fake_ctx
    worker.bot_audit = fake_audit
    worker._product_qty = fake_qty


def _exp_flow(tg="900001"):
    f = worker.flow_start(tg, "exp")
    f["data"].update({"branch": "Store A", "category": "Utilities", "amount": 50,
                      "account": "Cash", "memo": "", "receipt": None})
    f["step"] = len(worker.steps_for("exp")) - 1   # the confirm step
    return tg


# --------------------------------------------------------------- success card
def test_success_returns_confirmation_card_and_auto_return_marker():
    _mock()
    tg = _exp_flow()
    res = run(worker.flow_submit(tg))
    assert len(res) == 4, "success returns a 4-tuple carrying the auto-return marker"
    text, markup, doc, after = res
    assert "✅" in text and "Successfully Saved" in text
    assert "Reference:" in text and "#12345" in text
    assert "Date:" in text
    assert "recorded in SmokeStack ERP" in text
    assert after == {"then_home": worker.HOME_DELAY}      # ~2.5s auto-return
    assert doc is None                                     # no document → edits in place
    # the flow is finished (no lingering state)
    assert worker.flow(tg) is None


def test_success_card_format_matches_spec():
    card = worker.success_card("expense", "#777", ["Utilities: $50 @ Store A"])
    lines = card.split("\n")
    assert lines[0] == "✅ <b>Successfully Saved</b>"
    assert "Reference:" in lines and "<code>#777</code>" in lines
    assert "Date:" in lines
    assert lines[-1] == "The operation has been recorded in SmokeStack ERP."


# --------------------------------------------------------------- error card
def test_save_failure_keeps_screen_with_retry_cancel_back():
    _mock(exp_status=403, exp_data={"detail": "denied"})
    tg = _exp_flow()
    res = run(worker.flow_submit(tg))
    assert len(res) == 3, "errors must NOT carry the auto-return marker (stay on screen)"
    text, markup, doc = res
    assert "❌" in text and "Operation Failed" in text
    assert "not" in text.lower() and "saved" in text.lower()
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Retry" in l for l in labels)
    assert any("Cancel" in l for l in labels)
    assert any("Back" in l for l in labels)
    # no "Main Menu" button on an error — the user is not sent home
    assert not any("Main Menu" in l for l in labels)
    # the flow is still alive so Retry works
    assert worker.flow(tg) is not None


def test_server_error_also_shows_failure_card():
    _mock(exp_status=500, exp_data={"detail": "boom"})
    tg = _exp_flow()
    text, markup, doc = run(worker.flow_submit(tg))
    assert "Operation Failed" in text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Retry" in l for l in labels)


# --------------------------------------------------- on_callback: edit-in-place + auto-return, no dupes
class _Q:
    def __init__(self, tg, data):
        self.id = "cb1"
        self.data = data
        self.from_user = type("U", (), {"id": tg})()
        self.message = type("M", (), {"message_id": 55})()
        self.edits = []
    async def answer(self):
        return None
    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append(text)


class _Bot:
    def __init__(self):
        self.sends = []
    async def send_message(self, **k):
        self.sends.append(k)
    async def send_document(self, **k):
        self.sends.append(k)


class _App:
    def create_task(self, coro):
        return asyncio.ensure_future(coro)


class _Ctx:
    def __init__(self):
        self.bot = _Bot()
        self.application = _App()


class _Upd:
    def __init__(self, q):
        self.callback_query = q
        self.effective_chat = type("C", (), {"id": 900001})()
        self.effective_user = q.from_user


def test_on_callback_edits_in_place_then_auto_returns_no_duplicate():
    _mock()
    worker.HOME_DELAY = 0.05                     # keep the test fast
    tg = _exp_flow("900001")
    q = _Q("900001", "f:go")
    ctx = _Ctx()

    async def scenario():
        await worker.on_callback(_Upd(q), ctx)
        # first edit = the success card, in place (no new message)
        assert len(q.edits) == 1
        assert "Successfully Saved" in q.edits[0]
        assert ctx.bot.sends == [], "must not send a new message (no duplicates)"
        # let the scheduled auto-return fire
        await asyncio.sleep(0.2)
        # second edit = the main menu, still the SAME message
        assert len(q.edits) == 2
        assert q.edits[1] == worker.HOME_TEXT or "Dashboard" in q.edits[1]
        assert ctx.bot.sends == [], "auto-return edits, never sends a new message"

    run(scenario())


def test_on_callback_error_stays_put_and_does_not_auto_return():
    _mock(exp_status=403, exp_data={"detail": "no"})
    worker.HOME_DELAY = 0.05
    tg = _exp_flow("900002")
    q = _Q("900002", "f:go")
    ctx = _Ctx()

    async def scenario():
        await worker.on_callback(_Upd(q), ctx)
        assert len(q.edits) == 1 and "Operation Failed" in q.edits[0]
        await asyncio.sleep(0.2)
        # no auto-return: still exactly one edit, still on the error screen
        assert len(q.edits) == 1
        assert ctx.bot.sends == []

    run(scenario())


# --------------------------------------------------- existing behaviour unchanged
def test_other_operations_still_save_and_confirm():
    # receive stock: different payload, still yields a success card
    _mock(exp_data={"new_stock": 42})
    tg = "900003"
    f = worker.flow_start(tg, "recv")
    f["data"].update({"sku": "SKU1", "pname": "Widget", "branch": "Store A",
                      "qty": 10, "unit_cost": 3, "supplier": None, "invoice": None, "receipt": None})
    f["step"] = len(worker.steps_for("recv")) - 1
    res = run(worker.flow_submit(tg))
    assert len(res) == 4 and "Successfully Saved" in res[0]
    assert "New stock: 42" in res[0]
    assert res[3] == {"then_home": worker.HOME_DELAY}
