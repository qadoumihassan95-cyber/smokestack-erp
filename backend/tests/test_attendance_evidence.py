"""Telegram attendance evidence — attempt binding/expiry, reuse prevention,
photo-only, geofence flag, retention, RBAC review, and bot-token gating.
Telegram transport is not touched; the engine is exercised directly + via the
bot-token-gated endpoints with a mocked payload."""
import os
import tempfile
from datetime import datetime, timezone, timedelta

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_attev_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "test-secret"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app import models, attendance_evidence as AE  # noqa: E402

client = TestClient(app)
STORE_A = (32.221100, 35.254400)   # seed coordinates for Store A
BOT = "ATTBOT"


# SEC-15: selfie ingest now DECODES the bytes and re-encodes them, because the
# caller's declared content type is a claim and `image/svg+xml` starts with
# "image/". The stubs these tests used (`_jpeg()`) are not decodable
# images and are now correctly refused. A real 8x8 JPEG keeps every assertion below
# about the attendance FLOW — which is what this module is for — rather than about
# image validity, which `test_sec15_selfie_stored_active_content.py` owns.
def _jpeg():
    import io as _io

    from PIL import Image as _Image
    buf = _io.BytesIO()
    _Image.new("RGB", (8, 8), (120, 60, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _link(tg_id, uid="U-owner"):
    db = SessionLocal()
    try:
        db.query(models.TelegramLink).filter(models.TelegramLink.tg_id == str(tg_id)).delete()
        db.add(models.TelegramLink(tg_id=str(tg_id), user_id=uid, username="t", status="active"))
        db.commit()
    finally:
        db.close()


def _clear_active(uid="U-owner"):
    db = SessionLocal()
    try:
        db.query(models.Attendance).filter(models.Attendance.user_id == uid,
                                            models.Attendance.status == "active").delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _no_leaked_state():
    """This module clocks U-owner in as the test employee. Under CI's single
    process + shared SQLite DB, a leaked ACTIVE clock-in (or the telegram
    links/evidence rows created here) would poison later tests (e.g. a downstream
    clock-in seeing 409). Clean up our own footprint after every test so the
    module preserves shared-state isolation without weakening any assertion."""
    yield
    db = SessionLocal()
    try:
        db.query(models.Attendance).filter(models.Attendance.user_id == "U-owner",
                                            models.Attendance.status == "active").delete()
        db.query(models.AttendanceEvidence).filter(
            models.AttendanceEvidence.tg_id.like("9100%")).delete()
        db.query(models.TelegramLink).filter(models.TelegramLink.tg_id.like("9100%")).delete()
        db.commit()
    finally:
        db.close()


def test_happy_path_within_area_creates_attendance():
    with TestClient(app):
        _link("910001"); _clear_active()
        db = SessionLocal()
        try:
            ev, first = AE.start_attempt(db, "910001")
            assert ev.status == "pending_location" and first in (True, False)
            ev = AE.submit_location(db, "910001", ev.attempt_id, STORE_A[0], STORE_A[1], "m1")
            assert ev.status == "pending_selfie" and ev.branch == "Store A"
            assert ev.out_of_area is False and ev.dist_m is not None
            ev, rec = AE.submit_selfie(db, "910001", ev.attempt_id, "f1", "m2", "image/jpeg", _jpeg())
            assert ev.status == "complete" and ev.consumed is True
            assert rec.status == "active" and rec.branch == "Store A"
            assert rec.approval == "none"
        finally:
            db.close()


def test_out_of_area_flags_and_requires_approval():
    with TestClient(app):
        _link("910002"); _clear_active()
        db = SessionLocal()
        try:
            ev, _ = AE.start_attempt(db, "910002")
            ev = AE.submit_location(db, "910002", ev.attempt_id, 0.0, 0.0, "m10")  # far away
            assert ev.out_of_area is True
            ev, rec = AE.submit_selfie(db, "910002", ev.attempt_id, "f", "m11", "image/jpeg", _jpeg())
            assert rec.approval == "pending"
        finally:
            db.close()


def test_expired_attempt_cannot_complete():
    with TestClient(app):
        _link("910003"); _clear_active()
        db = SessionLocal()
        try:
            ev, _ = AE.start_attempt(db, "910003")
            ev.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            db.commit()
            with pytest.raises(AE.EvidenceError):
                AE.submit_location(db, "910003", ev.attempt_id, STORE_A[0], STORE_A[1], "m20")
        finally:
            db.close()


def test_photo_only_rejects_documents():
    with TestClient(app):
        _link("910004"); _clear_active()
        db = SessionLocal()
        try:
            ev, _ = AE.start_attempt(db, "910004")
            AE.submit_location(db, "910004", ev.attempt_id, STORE_A[0], STORE_A[1], "m30")
            with pytest.raises(AE.EvidenceError):
                AE.submit_selfie(db, "910004", ev.attempt_id, "f", "m31", "application/pdf", b"%PDF-1.4")
        finally:
            db.close()


def test_reused_location_message_rejected():
    with TestClient(app):
        _link("910005"); _clear_active()
        db = SessionLocal()
        try:
            ev1, _ = AE.start_attempt(db, "910005")
            AE.submit_location(db, "910005", ev1.attempt_id, STORE_A[0], STORE_A[1], "shared-msg")
            AE.submit_selfie(db, "910005", ev1.attempt_id, "f", "s1", "image/jpeg", _jpeg())
            _clear_active()
            ev2, _ = AE.start_attempt(db, "910005")
            with pytest.raises(AE.EvidenceError):   # same Telegram location message id reused
                AE.submit_location(db, "910005", ev2.attempt_id, STORE_A[0], STORE_A[1], "shared-msg")
        finally:
            db.close()


def test_consumed_attempt_is_idempotent_no_duplicate():
    with TestClient(app):
        _link("910006"); _clear_active()
        db = SessionLocal()
        try:
            ev, _ = AE.start_attempt(db, "910006")
            AE.submit_location(db, "910006", ev.attempt_id, STORE_A[0], STORE_A[1], "m40")
            ev, rec1 = AE.submit_selfie(db, "910006", ev.attempt_id, "f", "m41", "image/jpeg", _jpeg())
            ev2, rec2 = AE.submit_selfie(db, "910006", ev.attempt_id, "f", "m41", "image/jpeg", _jpeg())
            assert rec1.id == rec2.id  # replay returns same attendance, no duplicate
        finally:
            db.close()


def test_retention_purges_selfie_bytes():
    with TestClient(app):
        _link("910007"); _clear_active()
        db = SessionLocal()
        try:
            ev, _ = AE.start_attempt(db, "910007")
            AE.submit_location(db, "910007", ev.attempt_id, STORE_A[0], STORE_A[1], "m50")
            ev, _ = AE.submit_selfie(db, "910007", ev.attempt_id, "f", "m51", "image/jpeg", _jpeg())
            ev.retain_until = datetime.now(timezone.utc) - timedelta(days=1)
            db.commit()
            assert AE.purge_expired_selfies(db) >= 1
            db.refresh(ev)
            assert ev.selfie is None
        finally:
            db.close()


def test_bot_endpoints_fail_closed_without_token():
    from app.config import settings as s
    s.bot_token = BOT
    with TestClient(app):
        _link("910008"); _clear_active()
        # no token -> 403, no info
        assert client.post("/api/telegram/attendance/start", json={"tg_id": "910008"}).status_code == 403
        # valid token drives the flow
        r = client.post("/api/telegram/attendance/start", json={"tg_id": "910008"},
                        headers={"X-Bot-Token": BOT})
        assert r.status_code == 200 and r.json()["ok"] is True
        aid = r.json()["attempt_id"]
        r = client.post("/api/telegram/attendance/location",
                        json={"tg_id": "910008", "attempt_id": aid, "lat": STORE_A[0],
                              "lng": STORE_A[1], "msg_id": "e1"}, headers={"X-Bot-Token": BOT})
        assert r.json()["ok"] is True and r.json()["need"] == "selfie"
        r = client.post("/api/telegram/attendance/selfie", headers={"X-Bot-Token": BOT},
                        data={"tg_id": "910008", "attempt_id": aid, "file_id": "f", "msg_id": "e2"},
                        files={"file": ("s.jpg", _jpeg(), "image/jpeg")})
        assert r.json()["ok"] is True and r.json()["attendance_id"]


def test_review_rbac_branch_scoped():
    from app.config import settings as s
    s.bot_token = BOT
    with TestClient(app):
        _link("910009"); _clear_active()
        db = SessionLocal()
        try:
            ev, _ = AE.start_attempt(db, "910009")
            AE.submit_location(db, "910009", ev.attempt_id, STORE_A[0], STORE_A[1], "m60")
            ev, rec = AE.submit_selfie(db, "910009", ev.attempt_id, "f", "m61", "image/jpeg", _jpeg())
            aid, eid = rec.id, ev.id
        finally:
            db.close()

        def tok(uid):
            r = client.post("/api/auth/login", data={"username": uid, "password": "demo1234"})
            return {"Authorization": "Bearer " + r.json()["access_token"]}

        owner = tok("U-owner")
        r = client.get(f"/api/attendance/{aid}/evidence", headers=owner)
        assert r.status_code == 200 and r.json()["has_evidence"] is True
        # selfie served to an authorized reviewer
        assert client.get(f"/api/attendance/evidence/{eid}/selfie", headers=owner).status_code == 200
        # a non-review role (cashier) is denied
        assert client.get(f"/api/attendance/{aid}/evidence", headers=tok("U-cash")).status_code == 403
        assert client.get(f"/api/attendance/evidence/{eid}/selfie", headers=tok("U-cash")).status_code == 403
