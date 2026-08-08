"""Telegram worker branch DISPLAY names.

Internal branch keys (Store A/B/C) are immutable and must keep flowing through
every API call, callback payload and audit line. But user-facing Telegram text
must show the business display name (GM Tobacco Duncanville / GM Tobacco
Lancaster / Smoke Depot Waco), falling back to the key only when a display name
is not configured. These tests drive the real worker render helpers with the
network + Telegram bot mocked (same convention as test_attendance_worker_flow.py)
and assert both halves of that contract.
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "telegram_worker"))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")
os.environ.setdefault("SMOKESTACK_API_BASE", "http://x")

import worker  # noqa: E402
from datetime import date  # noqa: E402

LABELS = {"Store A": "GM Tobacco Duncanville",
          "Store B": "GM Tobacco Lancaster",
          "Store C": "Smoke Depot Waco"}
KEYS = ["Store A", "Store B", "Store C"]
DISPLAYS = list(LABELS.values())


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _no_key_leak(text):
    assert not any(k in text for k in KEYS), f"internal key leaked to user text: {text!r}"


def _set_labels(mapping=None):
    worker._BLABELS.clear()
    worker._BLABELS.update(mapping if mapping is not None else LABELS)
    worker._BLABELS_AT = 9e18  # far future → load_blabels won't refetch


# --------------------------------------------------------------- unit: blabel
def test_blabel_maps_keys_and_falls_back():
    _set_labels()
    assert worker.blabel("Store A") == "GM Tobacco Duncanville"
    assert worker.blabel("Store C") == "Smoke Depot Waco"
    # a key with no configured display name falls back to the key itself
    assert worker.blabel("Store Z") == "Store Z"
    # empty / None are passed through untouched
    assert worker.blabel("") == ""
    assert worker.blabel(None) is None


def test_load_blabels_pulls_from_canonical_endpoint():
    worker._BLABELS.clear()
    worker._BLABELS_AT = 0.0
    seen = {}

    async def fake_req(method, path, token=None, **k):
        seen["path"] = path
        return (200, dict(LABELS))

    orig = worker._req
    worker._req = fake_req
    try:
        run(worker.load_blabels("tok"))
    finally:
        worker._req = orig
    # reuses the single canonical labels endpoint (no second mapping table)
    assert seen["path"] == "/api/branches/labels"
    assert worker.blabel("Store B") == "GM Tobacco Lancaster"


# --------------------------------------------------------------- render helpers
def _patch(sales=None, dash=None, branches=None):
    async def fake_get_ctx(tg_id):
        return ("tok", {"id": "U-owner", "name": "Owner"}, {})

    async def fake_req(method, path, token=None, **k):
        if path.startswith("/api/sales"):
            return (200, sales or [])
        if path.startswith("/api/reports/dashboard"):
            return (200, dash or {})
        if path.startswith("/api/branches/labels"):
            return (200, dict(LABELS))
        if path.startswith("/api/branches"):
            return (200, branches if branches is not None else KEYS)
        return (200, {})

    worker.get_ctx = fake_get_ctx
    worker._req = fake_req


def test_sales_by_branch_shows_all_three_display_names():
    _set_labels()
    today = date.today().isoformat()
    rows = [{"branch": "Store A", "amount": 100, "date": today},
            {"branch": "Store B", "amount": 200, "date": today},
            {"branch": "Store C", "amount": 300, "date": today}]
    _patch(sales=rows)
    text, _kb, _ = run(worker.render_sales_branch("700001"))
    for disp in DISPLAYS:
        assert disp in text, f"missing display name {disp!r} in:\n{text}"
    _no_key_leak(text)


def test_branch_picker_labels_display_but_callbacks_keep_keys():
    _set_labels()
    _patch()
    text, markup, _ = run(worker.render_branches("700002"))
    # flatten inline-keyboard button (label, callback_data) pairs
    labels, callbacks = [], []
    for row in markup.inline_keyboard:
        for btn in row:
            labels.append(btn.text)
            callbacks.append(btn.callback_data)
    joined_labels = " ".join(labels)
    for disp in DISPLAYS:
        assert disp in joined_labels, f"picker missing {disp!r}"
    _no_key_leak(joined_labels)
    # callbacks MUST still carry the immutable internal keys
    assert "br:Store A" in callbacks
    assert "br:Store B" in callbacks
    assert "br:Store C" in callbacks


def test_branch_dashboard_header_uses_display_name():
    _set_labels()
    _patch(dash={"sales_today": 10, "expenses_today": 5, "profit_today": 5})
    text, _kb, _ = run(worker.render_branch("700003", "Store C"))
    assert "Smoke Depot Waco" in text
    _no_key_leak(text)


def test_missing_display_name_falls_back_to_key():
    # Only Store A configured; Store B has no display name yet.
    _set_labels({"Store A": "GM Tobacco Duncanville"})
    today = date.today().isoformat()
    rows = [{"branch": "Store A", "amount": 100, "date": today},
            {"branch": "Store B", "amount": 200, "date": today}]
    _patch(sales=rows)
    text, _kb, _ = run(worker.render_sales_branch("700004"))
    assert "GM Tobacco Duncanville" in text
    # unconfigured branch still shows *something* (the key) rather than blank
    assert "Store B" in text


# --------------------------------------------------------------- selfie fallback
class _File:
    async def download_as_bytearray(self):
        return bytearray(b"\xff\xd8selfiebytes")


class _Bot:
    def __init__(self):
        self.sends = []

    async def send_message(self, **k):
        self.sends.append(k)

    async def get_file(self, file_id):
        return _File()


class _Ctx:
    def __init__(self):
        self.bot = _Bot()


class _Photo:
    def __init__(self, file_id):
        self.file_id = file_id


class _Msg:
    def __init__(self, tg_id, location=None, photo=None, message_id=1):
        self.message_id = message_id
        self.location = location
        self.forward_date = None
        self.photo = photo
        self.replies = []

    async def reply_text(self, text, **k):
        self.replies.append(text)


class _Loc:
    def __init__(self, lat, lng):
        self.latitude = lat
        self.longitude = lng


class _CbUpdate:
    def __init__(self, tg_id):
        self.effective_chat = type("C", (), {"id": int(tg_id)})()
        self.effective_user = type("U", (), {"id": int(tg_id)})()


class _MsgUpdate:
    def __init__(self, tg_id, msg):
        self.effective_chat = type("C", (), {"id": int(tg_id)})()
        self.effective_user = type("U", (), {"id": int(tg_id)})()
        self.message = msg


def test_selfie_completion_falls_back_to_blabel_when_api_omits_display():
    """Even if the selfie endpoint returns only the internal key, the worker
    resolves the business display name locally and never shows Store A."""
    _set_labels()
    tg = "700005"
    calls = []

    async def fake_get_ctx(tg_id):
        return ("tok", {"id": "U-owner", "name": "Owner"}, {"att_consent": True})

    def fake_note_user(update):
        return None

    async def fake_bot_req(method, path, body=None):
        calls.append(path)
        if path.endswith("/attendance/start"):
            return (200, {"ok": True, "attempt_id": "att-1", "first_use": False})
        if path.endswith("/attendance/location"):
            return (200, {"ok": True, "need": "selfie"})
        return (200, {})

    async def fake_bot_upload(path, fields, file_bytes, filename, mime):
        # NOTE: no "branch_display" in the response — forces the local fallback
        return (200, {"ok": True, "attendance_id": 5, "branch": "Store A",
                      "out_of_area": False})

    worker.get_ctx = fake_get_ctx
    worker.note_user = fake_note_user
    worker._bot_req = fake_bot_req
    worker._bot_upload = fake_bot_upload
    ctx = _Ctx()

    async def scenario():
        await worker.handle_att_location_cb(_CbUpdate(tg), ctx, tg, "att:in")
        await worker.on_location(
            _MsgUpdate(tg, _Msg(tg, location=_Loc(32.2211, 35.2544), message_id=1)), ctx)
        pmsg = _Msg(tg, photo=[_Photo("f")], message_id=2)
        await worker.on_media(_MsgUpdate(tg, pmsg), ctx)
        joined = " ".join(pmsg.replies)
        assert "Clocked in" in joined
        assert "GM Tobacco Duncanville" in joined, joined
        _no_key_leak(joined)

    try:
        run(scenario())
    finally:
        worker.STATE.pop(str(tg), None)
