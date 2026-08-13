"""Regression for the Telegram clock-in stall (symptom B).

Production traceback (python-telegram-bot 21.6):
    File "worker.py", line 1654, in on_location
        live = not bool(update.message.forward_date)
    AttributeError: 'Message' object has no attribute 'forward_date'

PTB 20+ removed Message.forward_date (replaced by forward_origin), so on_location
crashed the instant a location was shared -> no selfie prompt, dead-end. These
tests exercise the real handlers with Telegram mocked only at the boundary.
"""
import os, time, types, asyncio

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")
import worker as W


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class Msg:
    """A Telegram Message stand-in WITHOUT forward_date (as in PTB 21)."""
    def __init__(self, lat, lng, mid=1, photo=False):
        self.location = types.SimpleNamespace(latitude=lat, longitude=lng)
        self.message_id = mid
        self.photo = ([types.SimpleNamespace(file_id="F1", file_size=1234)] if photo else [])
        self.document = None
        self.replies = []
    async def reply_text(self, text, **kw):
        self.replies.append(text)


class Upd:
    def __init__(self, msg, uid="a1"):
        self.message = msg
        self.effective_user = types.SimpleNamespace(id=uid, username="zz", full_name="ZZ Emp")
        self.effective_chat = types.SimpleNamespace(id=999)


class Ctx:
    def __init__(self, ba=b"\xff\xd8\xff\xe0jpegbytes"):
        async def _dl():
            return bytearray(ba)
        self.bot = types.SimpleNamespace(
            get_file=lambda fid: _fake_file(_dl),
            send_message=self._send)
        self.sent = []
    async def _send(self, **kw):
        self.sent.append(kw.get("text"))


def _fake_file(dl):
    async def _coro():
        return types.SimpleNamespace(download_as_bytearray=dl)
    return _coro()


# ------------------------------------------------ _is_forwarded (version-safe)
def test_is_forwarded_never_raises_and_detects_forwards():
    assert W._is_forwarded(types.SimpleNamespace()) is False            # no attrs, no crash
    assert W._is_forwarded(types.SimpleNamespace(forward_origin=object())) is True
    assert W._is_forwarded(types.SimpleNamespace(forward_date=123)) is True

    class Raiser:
        @property
        def forward_date(self): raise AttributeError("gone")
        @property
        def forward_origin(self): raise AttributeError("gone")
    assert W._is_forwarded(Raiser()) is False                           # swallowed, safe


# ------------------------------------------------ the crash regression
def test_on_location_does_not_crash_and_requests_selfie():
    tg = "loc1"; W.STATE.pop(tg, None)
    s = W.st(tg); s["att_await"] = "in"; s["att_ts"] = time.time(); s["ev_attempt"] = "ATT-1"
    calls = []

    async def fake_bot_req(method, path, body=None):
        calls.append((method, path))
        return 200, {"ok": True, "status": "pending_selfie", "need": "selfie", "branch": "Store A"}
    W._bot_req = fake_bot_req

    msg = Msg(32.2211, 35.2544)
    run(W.on_location(Upd(msg, tg), Ctx()))          # must NOT raise AttributeError

    assert any(p == "/api/telegram/attendance/location" for (_, p) in calls), \
        "location endpoint was never reached (handler crashed before it)"
    assert any("selfie" in r.lower() for r in msg.replies), "no selfie prompt after location"
    assert W.st(tg).get("ev_await_selfie") is True


def test_on_location_error_clears_state_no_deadend():
    tg = "loc2"; W.STATE.pop(tg, None)
    s = W.st(tg); s["att_await"] = "in"; s["att_ts"] = time.time(); s["ev_attempt"] = "ATT-2"

    async def boom(method, path, body=None):
        raise RuntimeError("network down")
    W._bot_req = boom

    msg = Msg(1.0, 2.0)
    run(W.on_location(Upd(msg, tg), Ctx()))          # wrapped: must not propagate
    # state cleared so the employee isn't stuck, and a safe message was sent
    st = W.st(tg)
    assert not st.get("att_await") and not st.get("ev_attempt") and not st.get("ev_await_selfie")
    assert msg.replies and "again" in msg.replies[-1].lower()


# ------------------------------------------------ selfie completes clock-in
def test_selfie_photo_completes_clock_in():
    tg = "sel1"; W.STATE.pop(tg, None)
    s = W.st(tg); s["ev_await_selfie"] = True; s["ev_attempt"] = "ATT-3"; s["att_ts"] = time.time()
    up = {}

    async def fake_upload(path, fields, file_bytes, filename, mime):
        up["path"] = path; up["fields"] = fields
        return 200, {"ok": True, "status": "complete", "attendance_id": 5,
                     "branch": "Store A", "branch_display": "GM Tobacco Duncanville"}
    W._bot_upload = fake_upload

    msg = Msg(0, 0, photo=True)
    run(W.on_media(Upd(msg, tg), Ctx()))
    assert up.get("path") == "/api/telegram/attendance/selfie"
    assert any("clocked in" in r.lower() for r in msg.replies)
    assert not W.st(tg).get("ev_await_selfie")       # state cleared on success


# ------------------------------------------------ restart resumability
def test_on_media_resumes_selfie_after_state_loss():
    tg = "res1"; W.STATE.pop(tg, None)               # in-memory state lost (restart)
    up = {}

    async def fake_bot_req(method, path, body=None):
        if "attendance/current" in path:
            return 200, {"ok": True, "status": "pending_selfie", "attempt_id": "ATT-R", "need": "selfie"}
        return 200, {"ok": True}

    async def fake_upload(path, fields, file_bytes, filename, mime):
        up["attempt"] = fields.get("attempt_id")
        return 200, {"ok": True, "status": "complete", "attendance_id": 6, "branch": "Store A"}
    W._bot_req = fake_bot_req
    W._bot_upload = fake_upload

    msg = Msg(0, 0, photo=True)
    run(W.on_media(Upd(msg, tg), Ctx()))
    assert up.get("attempt") == "ATT-R"              # resumed from the API's pending attempt
    assert any("clocked in" in r.lower() for r in msg.replies)
