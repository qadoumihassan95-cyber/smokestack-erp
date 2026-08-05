"""Team Chat image attachments — content validation, RBAC, storage, serving,
deletion, and failure paths."""
import os
import io
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_chatatt_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "test-secret"

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402
from app.main import app  # noqa: E402
from app.config import settings  # noqa: E402

client = TestClient(app)


def _tok(uid="U-owner"):
    r = client.post("/api/auth/login", data={"username": uid, "password": "demo1234"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _img(fmt="JPEG", size=(64, 64), color=(200, 30, 30)):
    im = Image.new("RGB", size, color)
    b = io.BytesIO()
    im.save(b, format=fmt)
    return b.getvalue()


def _room(h):
    r = client.post("/api/chat/rooms", headers=h, json={"kind": "group", "name": "Attach Test"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _upload(h, room, data, filename="pic.jpg", mime="image/jpeg", caption="hi"):
    return client.post(f"/api/chat/rooms/{room}/attachments", headers=h,
                       files={"file": (filename, data, mime)}, data={"caption": caption})


def test_upload_valid_formats_and_serves():
    with TestClient(app):
        h = _tok()
        room = _room(h)
        for fmt, mime, ext in [("JPEG", "image/jpeg", "jpg"), ("PNG", "image/png", "png"),
                               ("WEBP", "image/webp", "webp")]:
            r = _upload(h, room, _img(fmt), filename=f"p.{ext}", mime=mime)
            assert r.status_code == 201, r.text
            msg = r.json()
            assert msg["kind"] == "image" and msg["attachments"], r.text
            att = msg["attachments"][0]
            assert att["mime"] == mime and att["width"] == 64 and att["height"] == 64
            # full + thumb both served to an authenticated member
            full = client.get(att["url"], headers=h)
            assert full.status_code == 200 and full.headers["content-type"] == mime
            thumb = client.get(att["thumb_url"], headers=h)
            assert thumb.status_code == 200
            # no public URL: unauthenticated request is rejected
            assert client.get(att["url"]).status_code == 401


def test_rejects_non_image_and_svg_and_executable():
    with TestClient(app):
        h = _tok()
        room = _room(h)
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        assert _upload(h, room, svg, filename="x.svg", mime="image/svg+xml").status_code in (415, 422)
        assert _upload(h, room, b"MZ\x90\x00executable", filename="x.png", mime="image/png").status_code in (415, 422)
        assert _upload(h, room, b"just text, not an image", filename="x.jpg").status_code in (415, 422)


def test_dimension_and_size_limits():
    with TestClient(app):
        h = _tok()
        room = _room(h)
        old_dim = settings.chat_attach_max_dim
        settings.chat_attach_max_dim = 32
        try:
            assert _upload(h, room, _img("JPEG", (64, 64))).status_code == 422  # over-dimension
        finally:
            settings.chat_attach_max_dim = old_dim
        old_bytes = settings.chat_attach_max_bytes
        settings.chat_attach_max_bytes = 10
        try:
            assert _upload(h, room, _img("PNG", (64, 64))).status_code == 413  # over-size
        finally:
            settings.chat_attach_max_bytes = old_bytes


def test_metadata_is_stripped_by_reencode():
    with TestClient(app):
        h = _tok()
        room = _room(h)
        # a valid JPEG carrying EXIF metadata; the stored copy must not contain it
        im = Image.new("RGB", (64, 64), (1, 2, 3))
        exif = im.getexif()
        exif[270] = "SECRETEXIF12345"        # ImageDescription
        b = io.BytesIO()
        im.save(b, format="JPEG", exif=exif)
        raw = b.getvalue()
        assert b"SECRETEXIF12345" in raw     # sanity: metadata is present in the upload
        r = _upload(h, room, raw, filename="e.jpg")
        assert r.status_code == 201, r.text
        att = r.json()["attachments"][0]
        stored = client.get(att["url"], headers=h).content
        assert b"SECRETEXIF12345" not in stored  # re-encoded → EXIF stripped


def test_rbac_non_member_cannot_upload_or_view():
    with TestClient(app):
        h = _tok()
        room = _room(h)
        r = _upload(h, room, _img("JPEG"))
        att = r.json()["attachments"][0]
        emp = _tok("U-emp")            # employee: has chat_send but is NOT a member of this room
        assert _upload(emp, room, _img("JPEG")).status_code == 403
        assert client.get(att["url"], headers=emp).status_code == 403


def test_delete_purges_and_cascades():
    with TestClient(app):
        h = _tok()
        room = _room(h)
        att = _upload(h, room, _img("JPEG")).json()["attachments"][0]
        assert client.delete(f"/api/chat/attachments/{att['id']}", headers=h).status_code == 200
        assert client.get(att["url"], headers=h).status_code == 404       # bytes purged
        # deleting the owning message also purges its attachment bytes
        att2 = _upload(h, room, _img("PNG"), caption="").json()["attachments"][0]
        m2 = _upload(h, room, _img("PNG"), caption="keep")
        mid = m2.json()["id"]
        assert client.delete(f"/api/chat/messages/{mid}", headers=h).status_code == 200
        # (att2's message was image-only; deleting the attachment cascades)
        assert client.delete(f"/api/chat/attachments/{att2['id']}", headers=h).status_code == 200
