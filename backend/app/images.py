"""Image ingest validation — the ONE implementation.

SECURITY (SEC-15). Two image ingest paths existed and only one was safe. Chat
attachments validated real image CONTENT and stored a re-encoded copy, so a
script-bearing SVG was rejected outright. Telegram attendance selfies stored the
caller's bytes verbatim together with the caller's DECLARED ``mime``, and
``/api/attendance/evidence/{eid}/selfie`` echoed that declared type back —
so an SVG carrying script was served ``image/svg+xml`` **inline**, in the
application's own origin, to the manager reviewing the clock-in.

The two paths were not different by design; the second was written without the
first. Extracting the working one here means the next ingest path has an obvious
thing to call, and there is nowhere for a second implementation to diverge. Chat's
behaviour is unchanged — ``routers/chat.py`` delegates to this and maps the
rejection back to exactly the status codes and messages it raised before.

WHY CONTENT AND NOT THE DECLARED TYPE. A caller-supplied content type is a claim,
not evidence; ``image/jpeg`` on an SVG is one header edit. The format is DETECTED
by decoding, checked against an allow-list, and the bytes are re-encoded — which
also strips EXIF and any trailing polyglot payload. The mime returned is derived
from the detected format, never from the caller.
"""
import io

ALLOWED_IMAGE = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}

# The set a stored image may ever be SERVED as. Kept beside the ingest allow-list
# on purpose: the two must agree, and a serving path that consults this cannot be
# talked into `image/svg+xml` by a row written before ingest was fixed.
SERVEABLE_MIME = frozenset(ALLOWED_IMAGE.values())
DEFAULT_SERVE_MIME = "application/octet-stream"


class ImageRejected(Exception):
    """An upload that is not an acceptable image.

    Carries the HTTP status the API layer should use, so the one implementation can
    serve callers that raise different exception types (chat raises HTTPException,
    attendance raises EvidenceError) without either of them re-deciding what a
    malformed image means.
    """

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def process_image(raw: bytes, *, max_bytes: int, max_dim: int, thumb_dim: int = 0):
    """Validate real image content and return a clean, re-encoded copy.

    Returns ``(clean, thumb, mime, width, height, fmt)``. ``thumb`` is ``b""`` when
    ``thumb_dim`` is 0. Raises :class:`ImageRejected` for anything that is not a
    decodable image in the allow-list, or is oversized in bytes or dimensions.
    """
    from PIL import Image, UnidentifiedImageError

    if not raw:
        raise ImageRejected(422, "Empty file.")
    if len(raw) > max_bytes:
        raise ImageRejected(413, "Image is too large.")
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()                       # structural integrity
        im = Image.open(io.BytesIO(raw))     # reopen (verify() leaves it unusable)
        fmt = (im.format or "").upper()
    except UnidentifiedImageError:
        raise ImageRejected(422, "File is not a valid image.")
    except ImageRejected:
        raise
    except Exception:
        raise ImageRejected(422, "File is not a valid image.")
    if fmt not in ALLOWED_IMAGE:
        raise ImageRejected(415, "Only JPEG, PNG, and WebP images are allowed.")
    w, h = im.size
    if w <= 0 or h <= 0:
        raise ImageRejected(422, "Invalid image dimensions.")
    if w > max_dim or h > max_dim:
        raise ImageRejected(422, f"Image exceeds {max_dim}px per side.")

    save_fmt = "PNG" if fmt == "PNG" else ("WEBP" if fmt == "WEBP" else "JPEG")
    im = im.convert("RGBA" if save_fmt == "PNG" else "RGB")
    out = io.BytesIO()
    im.save(out, format=save_fmt, quality=88)   # re-encode → strips metadata/polyglot
    clean = out.getvalue()

    thumb = b""
    if thumb_dim:
        tim = im.copy()
        tim.thumbnail((thumb_dim, thumb_dim))
        tout = io.BytesIO()
        tim.save(tout, format=save_fmt, quality=80)
        thumb = tout.getvalue()
    return clean, thumb, ALLOWED_IMAGE[fmt], w, h, fmt


def safe_serve_mime(stored_mime) -> str:
    """The content type a stored image may be served as.

    Defence in depth, and the half that covers rows written BEFORE ingest was
    fixed: re-encoding at ingest makes new rows trustworthy, but a database that
    already contains an SVG stored verbatim is not made safe by that. A serving
    path that clamps to the allow-list refuses to echo `image/svg+xml` regardless
    of what is in the column.
    """
    m = (stored_mime or "").split(";")[0].strip().lower()
    return m if m in SERVEABLE_MIME else DEFAULT_SERVE_MIME
