"""Telegram worker attendance flow — location → selfie, bound to ONE attempt.

Exercises the real worker handlers (handle_att_location_cb → on_location →
on_media) with the network + Telegram bot mocked, verifying the UX contract the
API enforces server-side: clock-IN asks for the current location, THEN a freshly
captured selfie, binds both to a single attempt, and does not complete until both
are provided. Photo-only and out-of-area behaviours are covered too. No network,
no Telegram — matches the repo's worker-test convention (see test_tg_confirm.py).
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "telegram_worker"))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")
os.environ.setdefault("SMOKESTACK_API_BASE", "http://x")

import worker  # noqa: E402


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------- Telegram stubs
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


# --------------------------------------------------------------- API mocks
def _install(selfie_resp, calls):
    """Patch worker network + context so the flow runs offline. `calls` collects
    (path, body) so tests can assert the SAME attempt_id is threaded through."""
    async def fake_get_ctx(tg_id):
        return ("tok", {"id": "U-owner", "name": "Owner"}, {"att_consent": True})

    def fake_note_user(update):
        return None

    async def fake_bot_req(method, path, body=None):
        calls.append((path, body))
        if path.endswith("/attendance/start"):
            return (200, {"ok": True, "attempt_id": "att-1", "first_use": False})
        if path.endswith("/attendance/location"):
            return (200, {"ok": True, "need": "selfie"})
        return (200, {})

    async def fake_bot_upload(path, fields, file_bytes, filename, mime):
        calls.append((path, dict(fields)))
        return (200, selfie_resp)

    worker.get_ctx = fake_get_ctx
    worker.note_user = fake_note_user
    worker._bot_req = fake_bot_req
    worker._bot_upload = fake_bot_upload


def _cleanup(tg_id):
    worker.STATE.pop(str(tg_id), None)


# --------------------------------------------------------------- tests
def test_clockin_requests_location_then_selfie_bound_to_one_attempt():
    tg = "920001"
    calls = []
    _install({"ok": True, "attendance_id": 7, "branch": "Store A",
              "branch_display": "GM Tobacco Duncanville", "out_of_area": False}, calls)
    ctx = _Ctx()

    async def scenario():
        # 1) Clock-IN pressed → starts an attempt and asks for location (not selfie yet).
        await worker.handle_att_location_cb(_CbUpdate(tg), ctx, tg, "att:in")
        s = worker.st(tg)
        assert s.get("ev_attempt") == "att-1", "an attempt was started and bound"
        assert s.get("att_await") == "in"
        assert not s.get("ev_await_selfie"), "selfie is NOT requested before the location"
        assert any("Share Current Location" in (k.get("text") or "") for k in ctx.bot.sends)

        # 2) Location shared → recorded on the SAME attempt, THEN selfie requested.
        msg = _Msg(tg, location=_Loc(32.2211, 35.2544), message_id=11)
        await worker.on_location(_MsgUpdate(tg, msg), ctx)
        s = worker.st(tg)
        assert s.get("ev_await_selfie") is True, "clock-in still incomplete — selfie required"
        assert "selfie" in " ".join(msg.replies).lower()

        # 3) Selfie photo sent → clock-in completes.
        pmsg = _Msg(tg, photo=[_Photo("file-xyz")], message_id=12)
        await worker.on_media(_MsgUpdate(tg, pmsg), ctx)
        assert any("Clocked in" in r for r in pmsg.replies)
        s = worker.st(tg)
        assert not s.get("ev_await_selfie") and not s.get("ev_attempt"), "attempt consumed"

        # The three API calls all carried the one bound attempt id.
        paths = [p for p, _ in calls]
        assert paths == ["/api/telegram/attendance/start",
                         "/api/telegram/attendance/location",
                         "/api/telegram/attendance/selfie"], paths
        loc_body = calls[1][1]
        sel_fields = calls[2][1]
        assert loc_body["attempt_id"] == "att-1"
        assert sel_fields["attempt_id"] == "att-1"
        assert loc_body["msg_id"] == "11" and sel_fields["msg_id"] == "12"

    try:
        run(scenario())
    finally:
        _cleanup(tg)


def test_clockin_does_not_complete_without_a_photo_selfie():
    tg = "920002"
    calls = []
    _install({"ok": True, "attendance_id": 8, "branch": "Store A"}, calls)
    ctx = _Ctx()

    async def scenario():
        await worker.handle_att_location_cb(_CbUpdate(tg), ctx, tg, "att:in")
        await worker.on_location(_MsgUpdate(tg, _Msg(tg, location=_Loc(32.2211, 35.2544), message_id=1)), ctx)
        # A non-photo message while awaiting the selfie must be rejected, not completed.
        doc_msg = _Msg(tg, photo=None, message_id=2)
        await worker.on_media(_MsgUpdate(tg, doc_msg), ctx)
        assert any("photo" in r.lower() for r in doc_msg.replies), "photo-only enforced"
        assert not any("Clocked in" in r for r in doc_msg.replies)
        assert worker.st(tg).get("ev_await_selfie") is True, "still awaiting a real selfie"
        # selfie endpoint must NOT have been called yet
        assert not any(p.endswith("/attendance/selfie") for p, _ in calls)

    try:
        run(scenario())
    finally:
        _cleanup(tg)


def test_out_of_area_selfie_reports_manager_approval():
    tg = "920003"
    calls = []
    _install({"ok": True, "attendance_id": 9, "branch": "Store A",
              "branch_display": "GM Tobacco Duncanville", "out_of_area": True}, calls)
    ctx = _Ctx()

    async def scenario():
        await worker.handle_att_location_cb(_CbUpdate(tg), ctx, tg, "att:in")
        await worker.on_location(_MsgUpdate(tg, _Msg(tg, location=_Loc(0.0, 0.0), message_id=1)), ctx)
        pmsg = _Msg(tg, photo=[_Photo("f")], message_id=2)
        await worker.on_media(_MsgUpdate(tg, pmsg), ctx)
        joined = " ".join(pmsg.replies)
        assert "Clocked in" in joined and "approval" in joined.lower()

    try:
        run(scenario())
    finally:
        _cleanup(tg)
