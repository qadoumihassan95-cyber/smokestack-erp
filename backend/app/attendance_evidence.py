"""Telegram attendance evidence — bind a location + a freshly captured selfie to
one short-lived, single-use clock-in attempt.

Flow (driven by the Telegram worker, one attempt at a time):
    start_attempt   -> status pending_location   (privacy notice on first use)
    submit_location -> status pending_selfie      (distance vs branch geofence)
    submit_selfie   -> status complete            (creates the Attendance row)

Guarantees:
  * The attempt expires quickly (settings.att_attempt_ttl_min) and is single-use
    (consumed) — an expired/consumed attempt can never complete a clock-in.
  * Reused locations, selfies, or Telegram message ids are rejected.
  * Selfie bytes live in Postgres; served only via authenticated, branch-scoped
    endpoints; never logged, never a public URL.
  * We do NOT claim GPS or a selfie proves identity or prevents spoofing — the
    location is checked against the configured geofence and flagged when outside.

This module is transport-agnostic and pure of FastAPI, so it is fully unit
testable with a mocked Telegram transport.
"""
import math
import secrets
import hashlib
from datetime import datetime, timedelta, timezone

from . import images, models
from .config import settings


class EvidenceError(Exception):
    """Raised for any invalid/expired/reused/consumed attempt (fail-closed)."""


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def haversine_m(lat1, lng1, lat2, lng2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _resolve_user(db, tg_id):
    link = db.get(models.TelegramLink, (tg_id or "").strip())
    if not link or (link.status or "active") != "active":
        raise EvidenceError("This Telegram account is not linked or is disabled.")
    u = db.get(models.User, link.user_id)
    if not u or u.status != "active":
        raise EvidenceError("The linked account is not active.")
    return u


def _scoped_branches(db, user):
    from . import permissions as P
    allb = [b.name for b in db.query(models.Branch).order_by(models.Branch.name).all()]
    return P.allowed_branches(user, allb)


def expire_stale(db, tg_id=None):
    """Mark timed-out pending attempts as expired (opportunistic housekeeping)."""
    q = db.query(models.AttendanceEvidence).filter(
        models.AttendanceEvidence.status.in_(("pending_location", "pending_selfie")),
        models.AttendanceEvidence.consumed == False)  # noqa: E712
    if tg_id:
        q = q.filter(models.AttendanceEvidence.tg_id == str(tg_id))
    now = _now()
    for ev in q.all():
        if _aware(ev.expires_at) and _aware(ev.expires_at) < now:
            ev.status = "expired"
    db.flush()


def current_pending(db, tg_id):
    """Return this Telegram id's single live attempt (pending_location or
    pending_selfie), or None. Lets the worker resume the flow after a restart
    that lost its in-memory state, so a location->selfie sequence is never a
    dead-end. Read-only + opportunistically expires stale attempts first."""
    tg_id = str(tg_id or "").strip()
    if not tg_id:
        return None
    expire_stale(db, tg_id)
    ev = (db.query(models.AttendanceEvidence)
          .filter(models.AttendanceEvidence.tg_id == tg_id,
                  models.AttendanceEvidence.consumed == False,  # noqa: E712
                  models.AttendanceEvidence.status.in_(("pending_location", "pending_selfie")))
          .order_by(models.AttendanceEvidence.id.desc()).first())
    return ev


def start_attempt(db, tg_id):
    """Begin a fresh attempt. Cancels any prior unfinished attempt for this
    Telegram id so only one is ever live. Returns (evidence, first_use)."""
    tg_id = str(tg_id or "").strip()
    user = _resolve_user(db, tg_id)
    expire_stale(db, tg_id)
    # supersede any still-pending attempt
    for ev in db.query(models.AttendanceEvidence).filter(
            models.AttendanceEvidence.tg_id == tg_id,
            models.AttendanceEvidence.status.in_(("pending_location", "pending_selfie")),
            models.AttendanceEvidence.consumed == False).all():  # noqa: E712
        ev.status = "cancelled"
    first_use = db.query(models.AttendanceEvidence).filter(
        models.AttendanceEvidence.employee_id == user.id).first() is None
    now = _now()
    ev = models.AttendanceEvidence(
        attempt_id=secrets.token_urlsafe(24), employee_id=user.id, employee_name=user.name,
        tg_id=tg_id, status="pending_location",
        created_at=now, expires_at=now + timedelta(minutes=settings.att_attempt_ttl_min))
    # SEC HIGH-10: registering attendance_evidence as tenant-owned makes _stamp_writes
    # fill this in for AUTHENTICATED requests, but this path is bot-driven — the caller
    # presents a bot token, not a session, so the session carries no company context
    # and the row would still fall back to the company_id server default of 1. Take it
    # from the employee the tg_id actually resolves to, which is the only tenant this
    # attempt can belong to.
    ev.company_id = getattr(user, "company_id", None) or 1
    db.add(ev)
    db.commit()
    return ev, first_use


def _load_active(db, tg_id, attempt_id, want_status):
    ev = db.query(models.AttendanceEvidence).filter(
        models.AttendanceEvidence.attempt_id == (attempt_id or "")).first()
    if not ev:
        raise EvidenceError("Unknown attendance attempt.")
    if str(ev.tg_id) != str(tg_id):
        raise EvidenceError("This attempt does not belong to you.")
    if ev.consumed:
        raise EvidenceError("This attempt was already completed.")
    if _aware(ev.expires_at) and _aware(ev.expires_at) < _now():
        ev.status = "expired"
        db.commit()
        raise EvidenceError("This attempt has expired — please start again.")
    if ev.status != want_status:
        raise EvidenceError(f"This attempt is not awaiting {want_status.replace('pending_', '')}.")
    return ev


def submit_location(db, tg_id, attempt_id, lat, lng, msg_id, live=True):
    if lat is None or lng is None or abs(lat) > 90 or abs(lng) > 180:
        raise EvidenceError("Invalid coordinates.")
    if live is False:
        raise EvidenceError("Please share your current location, not a forwarded pin.")
    ev = _load_active(db, tg_id, attempt_id, "pending_location")
    if ev.loc_msg_id is not None:
        raise EvidenceError("A location was already recorded for this attempt.")
    # reuse guard: this Telegram message id must not have been used before
    if msg_id and db.query(models.AttendanceEvidence).filter(
            models.AttendanceEvidence.loc_msg_id == str(msg_id)).first():
        raise EvidenceError("This location message was already used.")
    user = _resolve_user(db, tg_id)
    # nearest scoped branch with coordinates → distance + geofence flag
    best = None
    for name in _scoped_branches(db, user):
        b = db.get(models.Branch, name)
        if not b or b.lat is None or b.lng is None or b.attendance_active is False:
            continue
        d = int(round(haversine_m(lat, lng, float(b.lat), float(b.lng))))
        if best is None or d < best[1]:
            best = (b, d)
    if best is None:
        raise EvidenceError("No authorized branch has attendance coordinates set.")
    b, dist = best
    ev.branch = b.name
    ev.lat, ev.lng, ev.dist_m = lat, lng, dist
    ev.loc_msg_id = str(msg_id) if msg_id else None
    ev.loc_at = _now()
    ev.out_of_area = bool(b.loc_verify is not False and dist > int(b.radius_m or 150))
    ev.status = "pending_selfie"
    db.commit()
    return ev


def submit_selfie(db, tg_id, attempt_id, file_id, msg_id, mime, raw_bytes):
    """Attach the freshly captured selfie and complete the clock-in. Idempotent:
    a re-delivered selfie for an already-completed attempt returns the same
    attendance record without creating a duplicate."""
    ev = db.query(models.AttendanceEvidence).filter(
        models.AttendanceEvidence.attempt_id == (attempt_id or "")).first()
    if ev and ev.consumed and ev.status == "complete":
        return ev, db.get(models.Attendance, ev.attendance_id)   # idempotent replay
    ev = _load_active(db, tg_id, attempt_id, "pending_selfie")
    # SECURITY (SEC-15). `mime` is the caller's DECLARED content type and was the
    # only check here — `startswith("image/")` is satisfied by `image/svg+xml`, and
    # by `image/jpeg` on any bytes at all. The raw bytes were then stored verbatim
    # and the declared type stored beside them, so a scripted SVG was later served
    # back `image/svg+xml` INLINE to the reviewing manager, in our own origin.
    #
    # The format is now DETECTED by decoding and the bytes are re-encoded, using the
    # same implementation the chat upload path already used (`app/images.py`). The
    # stored mime is derived from what the image actually is, never from the caller.
    # Re-encoding also strips EXIF — which for a clock-in selfie includes the
    # phone's own GPS tags, a second copy of the employee's location that nothing
    # in this flow needs.
    if not (mime or "").lower().startswith("image/"):
        raise EvidenceError("Please send a photo (a selfie), not a file or document.")
    if not raw_bytes:
        raise EvidenceError("Empty photo.")
    if len(raw_bytes) > settings.att_selfie_max_bytes:
        raise EvidenceError("The photo is too large.")
    try:
        raw_bytes, _thumb, mime, _w, _h, _fmt = images.process_image(
            raw_bytes,
            max_bytes=settings.att_selfie_max_bytes,
            max_dim=settings.att_selfie_max_dim)
    except images.ImageRejected as e:
        raise EvidenceError("Please send a photo (a selfie), not a file or document."
                            if e.status in (415, 422) else e.message)
    if msg_id and db.query(models.AttendanceEvidence).filter(
            models.AttendanceEvidence.selfie_msg_id == str(msg_id)).first():
        raise EvidenceError("This photo message was already used.")
    user = _resolve_user(db, tg_id)
    # cannot open a second active clock-in
    if db.query(models.Attendance).filter(models.Attendance.user_id == user.id,
                                          models.Attendance.status == "active").first():
        raise EvidenceError("You already have an active clock-in. Clock out first.")
    now = _now()
    ev.selfie = raw_bytes
    ev.selfie_file_id = str(file_id) if file_id else None
    ev.selfie_msg_id = str(msg_id) if msg_id else None
    ev.selfie_mime = mime
    ev.selfie_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    ev.selfie_at = now
    ev.retain_until = now + timedelta(days=settings.att_selfie_retention_days)
    # create the attendance clock-in row bound to this evidence
    rec = models.Attendance(
        user_id=user.id, employee_id=user.id, employee_name=user.name, tg_id=str(tg_id),
        branch=ev.branch, clock_in_at=now, ci_lat=ev.lat, ci_lng=ev.lng, ci_dist=ev.dist_m,
        status="active", approval=("pending" if ev.out_of_area else "none"),
        reason=("Outside permitted area" if ev.out_of_area else None), source="TELEGRAM")
    # SEC-09 class (found by sweeping the class, not reported): `attendance` is a
    # tenant table and this is the BOT path — a bot token, no session, no company
    # context — so `_stamp_writes` had nothing to apply and every Telegram clock-in,
    # for every tenant, was recorded as Company 1's attendance. Payroll-relevant data,
    # silently filed under the wrong company. The tenant is the employee's, exactly as
    # for the evidence row this clock-in is bound to.
    rec.company_id = (getattr(user, "company_id", None)
                      or getattr(ev, "company_id", None) or 1)
    db.add(rec)
    db.flush()
    ev.attendance_id = rec.id
    ev.status = "complete"
    ev.consumed = True
    db.commit()
    return ev, rec


def cancel_attempt(db, tg_id, attempt_id):
    ev = db.query(models.AttendanceEvidence).filter(
        models.AttendanceEvidence.attempt_id == (attempt_id or "")).first()
    if ev and str(ev.tg_id) == str(tg_id) and not ev.consumed:
        ev.status = "cancelled"
        db.commit()
    return ev


def purge_expired_selfies(db):
    """Retention: null out selfie bytes past their retain_until (deletion)."""
    now = _now()
    n = 0
    for ev in db.query(models.AttendanceEvidence).filter(
            models.AttendanceEvidence.selfie.isnot(None),
            models.AttendanceEvidence.retain_until.isnot(None)).all():
        if _aware(ev.retain_until) and _aware(ev.retain_until) < now:
            ev.selfie = None
            n += 1
    db.commit()
    return n


PRIVACY_NOTICE = (
    "📸 Attendance check-in uses your *current location* and a *live selfie* to "
    "confirm you're on-site. These are stored securely and visible only to your "
    "managers/owner for your branch. Location + selfie are supporting evidence — "
    "they do not by themselves prove identity. Selfies are auto-deleted after the "
    "retention period. Continue?"
)
