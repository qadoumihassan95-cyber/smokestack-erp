"""Employee attendance via geofenced clock-in/out. Reuses users, branches,
permissions, Telegram linking and the audit log. Location is only ever supplied
by an explicit clock-in / clock-out call — never tracked continuously."""
import math
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..database import get_db
from ..config import settings
from .. import models, security as S, permissions as P

router = APIRouter(prefix="/api/attendance", tags=["attendance"])


def haversine(lat1, lng1, lat2, lng2):
    """Great-circle distance in metres."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt):
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def _tg_id(db, user):
    link = db.query(models.TelegramLink).filter(models.TelegramLink.user_id == user.id).first()
    return link.tg_id if link else None


def _serialize(a):
    return {"id": a.id, "user_id": a.user_id, "employee": a.employee_name, "tg_id": a.tg_id,
            "branch": a.branch, "clock_in": _iso(a.clock_in_at), "clock_out": _iso(a.clock_out_at),
            "ci_lat": float(a.ci_lat) if a.ci_lat is not None else None,
            "ci_lng": float(a.ci_lng) if a.ci_lng is not None else None,
            "ci_dist": a.ci_dist, "co_dist": a.co_dist,
            "status": a.status, "approval": a.approval, "approver": a.approver,
            "approved_at": _iso(a.approved_at), "reason": a.reason, "late": bool(a.late),
            "worked_minutes": a.worked_minutes, "source": a.source}


def _candidates(db, branches, lat, lng):
    out = []
    for name in branches:
        b = db.get(models.Branch, name)
        if not b or b.lat is None or b.lng is None:
            continue
        if b.attendance_active is False:
            continue
        d = haversine(lat, lng, float(b.lat), float(b.lng))
        out.append((b, int(round(d))))
    out.sort(key=lambda x: x[1])
    return out


class GeoIn(BaseModel):
    lat: float
    lng: float
    branch: Optional[str] = None
    reason: Optional[str] = None
    live: Optional[bool] = True   # True = came from a request_location share (not a forwarded/typed pin)


def _valid(body: GeoIn):
    return body.lat is not None and body.lng is not None and abs(body.lat) <= 90 and abs(body.lng) <= 180


@router.post("/clock-in")
def clock_in(body: GeoIn, db: Session = Depends(get_db), user: models.User = Depends(S.get_current_user)):
    if user.status != "active":
        raise HTTPException(403, "Your account is not active.")
    if not _valid(body):
        raise HTTPException(422, "Invalid coordinates.")
    if body.live is False:
        raise HTTPException(422, "Please share your current location, not a forwarded pin.")
    branches = S.scope_branches(user, db)
    if not branches:
        raise HTTPException(400, "You have no assigned branch.")
    if db.query(models.Attendance).filter(models.Attendance.user_id == user.id,
                                          models.Attendance.status == "active").first():
        raise HTTPException(409, "You already have an active clock-in. Clock out first.")
    near = _candidates(db, branches, body.lat, body.lng)
    if not near:
        raise HTTPException(400, "No authorized branch has attendance coordinates set.")
    within = [(b, d) for (b, d) in near if d <= int(b.radius_m or 150) or b.loc_verify is False]
    tg = _tg_id(db, user)
    now = datetime.now(timezone.utc)
    # multi-branch: more than one within range and none chosen -> ask the user to pick
    if body.branch is None and len(within) > 1:
        return {"result": "choose",
                "candidates": [{"branch": b.name, "distance": d} for (b, d) in within]}
    if body.branch:
        sel = next(((b, d) for (b, d) in near if b.name == body.branch), None)
        if not sel:
            raise HTTPException(403, "That branch is not authorized for you.")
    else:
        sel = within[0] if within else near[0]
    b, dist = sel
    radius = int(b.radius_m or 150)
    ok = dist <= radius or b.loc_verify is False

    def make(status, approval, reason=None):
        rec = models.Attendance(user_id=user.id, employee_id=user.id, employee_name=user.name, tg_id=tg,
                                branch=b.name, clock_in_at=now, ci_lat=body.lat, ci_lng=body.lng, ci_dist=dist,
                                status=status, approval=approval, reason=reason, late=False, source="TELEGRAM")
        db.add(rec); db.commit()
        return rec

    if ok:
        rec = make("active", "none")
        S.audit(db, user, "clock_in", "attendance", rec.id, f"{b.name} {dist}m", source="TELEGRAM")
        return {"result": "in", "status": "active", "id": rec.id, "branch": b.name, "distance": dist,
                "radius": radius, "time": _iso(now), "late": False,
                "candidates": [{"branch": x[0].name, "distance": x[1]} for x in near]}
    if b.allow_override is not False:
        rec = make("pending", "pending", reason=body.reason)
        S.audit(db, user, "clock_in_pending", "attendance", rec.id,
                f"{b.name} {dist}m > {radius}m (approval requested)", source="TELEGRAM")
        return {"result": "pending", "status": "pending", "id": rec.id, "branch": b.name,
                "distance": dist, "radius": radius, "time": _iso(now)}
    S.audit(db, user, "clock_in_denied", "attendance", "", f"{b.name} {dist}m > {radius}m",
            result="fail", source="TELEGRAM")
    return {"result": "outside", "branch": b.name, "distance": dist, "radius": radius,
            "candidates": [{"branch": x[0].name, "distance": x[1]} for x in near]}


@router.post("/clock-out")
def clock_out(body: GeoIn, db: Session = Depends(get_db), user: models.User = Depends(S.get_current_user)):
    if not _valid(body):
        raise HTTPException(422, "Invalid coordinates.")
    rec = db.query(models.Attendance).filter(models.Attendance.user_id == user.id,
                                             models.Attendance.status == "active").first()
    if not rec:
        raise HTTPException(409, "You have no active clock-in.")
    b = db.get(models.Branch, rec.branch)
    dist = int(round(haversine(body.lat, body.lng, float(b.lat), float(b.lng)))) if (b and b.lat is not None) else None
    now = datetime.now(timezone.utc)
    rec.clock_out_at = now
    rec.co_lat, rec.co_lng, rec.co_dist = body.lat, body.lng, dist
    rec.status = "completed"
    rec.worked_minutes = int((now - _aware(rec.clock_in_at)).total_seconds() // 60)
    db.commit()
    S.audit(db, user, "clock_out", "attendance", rec.id, f"{rec.branch} worked {rec.worked_minutes}m",
            source="TELEGRAM")
    return {"result": "out", "branch": rec.branch, "clock_in": _iso(rec.clock_in_at),
            "clock_out": _iso(now), "worked_minutes": rec.worked_minutes, "distance": dist}


@router.get("/today")
def today(db: Session = Depends(get_db), user: models.User = Depends(S.get_current_user)):
    rec = (db.query(models.Attendance).filter(models.Attendance.user_id == user.id)
           .order_by(models.Attendance.id.desc()).first())
    if not rec:
        return {"state": "none"}
    return {"state": rec.status if rec.approval != "pending" else "pending", **_serialize(rec)}


@router.get("/me")
def my_attendance(period: str = "today", db: Session = Depends(get_db),
                  user: models.User = Depends(S.get_current_user)):
    q = db.query(models.Attendance).filter(models.Attendance.user_id == user.id)
    rows = q.order_by(models.Attendance.id.desc()).limit(60).all()
    today = datetime.now(timezone.utc).date()

    def keep(a):
        d = _aware(a.clock_in_at)
        if not d:
            return False
        if period == "today":
            return d.date() == today
        if period == "week":
            return (today - d.date()).days < 7
        if period == "month":
            return d.date().year == today.year and d.date().month == today.month
        return True

    return [_serialize(a) for a in rows if keep(a)]


@router.get("/pending")
def pending(db: Session = Depends(get_db), user: models.User = Depends(S.require("approve"))):
    brs = S.scope_branches(user, db)
    rows = (db.query(models.Attendance)
            .filter(models.Attendance.approval == "pending", models.Attendance.branch.in_(brs)).all())
    return [_serialize(a) for a in rows]


@router.post("/{aid}/approve")
def approve(aid: int, db: Session = Depends(get_db), user: models.User = Depends(S.require("approve"))):
    a = db.get(models.Attendance, aid)
    if not a:
        raise HTTPException(404, "Not found")
    S.assert_branch(user, db, a.branch)
    a.approval = "approved"
    a.status = "active"           # becomes a valid active session the employee can clock out of
    a.approver = user.name
    a.approved_at = datetime.now(timezone.utc)
    db.commit()
    S.audit(db, user, "attendance_approved", "attendance", aid, a.branch)
    return {"ok": True, "id": aid, "status": "active", "tg_id": a.tg_id,
            "branch": a.branch, "employee": a.employee_name}


@router.post("/{aid}/reject")
def reject(aid: int, db: Session = Depends(get_db), user: models.User = Depends(S.require("approve"))):
    a = db.get(models.Attendance, aid)
    if not a:
        raise HTTPException(404, "Not found")
    S.assert_branch(user, db, a.branch)
    a.approval = "rejected"
    a.status = "rejected"
    a.approver = user.name
    a.approved_at = datetime.now(timezone.utc)
    db.commit()
    S.audit(db, user, "attendance_rejected", "attendance", aid, a.branch)
    return {"ok": True, "id": aid, "status": "rejected", "tg_id": a.tg_id,
            "branch": a.branch, "employee": a.employee_name}


@router.get("/approvers")
def approvers(branch: str, x_bot_token: str = Header(None), db: Session = Depends(get_db)):
    """Linked Telegram ids of users who can approve attendance for a branch —
    used by the bot to notify managers. Bot-token gated."""
    if not settings.bot_token or x_bot_token != settings.bot_token:
        raise HTTPException(403, "Forbidden")
    all_br = [b.name for b in db.query(models.Branch).all()]
    out = []
    for link in db.query(models.TelegramLink).all():
        u = db.get(models.User, link.user_id)
        if not u or u.status != "active":
            continue
        if P.can(u.role, "approve") and branch in P.allowed_branches(u, all_br):
            out.append({"tg_id": link.tg_id, "name": u.name})
    return {"approvers": out}


@router.get("")
def attendance_list(branch: str = "all", db: Session = Depends(get_db),
                    user: models.User = Depends(S.require("view"))):
    brs = S.scope_branches(user, db) if branch == "all" else [branch]
    rows = (db.query(models.Attendance).filter(models.Attendance.branch.in_(brs))
            .order_by(models.Attendance.id.desc()).limit(200).all())
    return [_serialize(a) for a in rows]


def _hm_to_min(hm):
    try:
        h, m = str(hm or "").split(":")
        return int(h) * 60 + int(m)
    except Exception:  # noqa: BLE001
        return None


@router.get("/worksheet")
def worksheet(period: str = "month", start: str = None, end: str = None, branch: str = "all",
              employee: str = "all", status: str = "all",
              db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    """Employee work schedule + Telegram attendance: joins each attendance punch with the
    employee's scheduled shift to compute late minutes, worked hours and overtime."""
    brs = S.scope_branches(user, db) if branch == "all" else [branch]
    today = datetime.now(timezone.utc).date()
    # date window
    if start and end:
        try:
            d0, d1 = datetime.fromisoformat(start).date(), datetime.fromisoformat(end).date()
        except Exception:  # noqa: BLE001
            d0 = d1 = today
    elif period == "today":
        d0 = d1 = today
    elif period == "week":
        from datetime import timedelta
        d0, d1 = today - timedelta(days=today.weekday()), today
    elif period == "month":
        d0, d1 = today.replace(day=1), today
    else:
        d0, d1 = today.replace(month=1, day=1), today

    # schedules by employee name (best-effort join; attendance stores employee_name)
    emps = {e.name: e for e in db.query(models.Employee).filter(models.Employee.branch.in_(brs)).all()}
    q = (db.query(models.Attendance).filter(models.Attendance.branch.in_(brs))
         .order_by(models.Attendance.id.desc()).limit(500).all())
    out = []
    for a in q:
        d = _aware(a.clock_in_at)
        if d and not (d0 <= d.date() <= d1):
            continue
        if status != "all" and a.status != status:
            continue
        if employee != "all" and (a.employee_name or "") != employee:
            continue
        e = emps.get(a.employee_name)
        ss = (e.sched_start if e else None) or "09:00"
        se = (e.sched_end if e else None) or "17:00"
        sched_min = (_hm_to_min(se) or 0) - (_hm_to_min(ss) or 0)
        late_min = 0
        if d is not None:
            ci_min = d.hour * 60 + d.minute
            b = db.get(models.Branch, a.branch)
            grace = int((b.grace_min if b else 10) or 10)
            late_min = max(0, ci_min - (_hm_to_min(ss) or 0) - grace)
        worked = a.worked_minutes or 0
        overtime = max(0, worked - sched_min) if (worked and sched_min > 0) else 0
        out.append({
            "id": a.id, "employee": a.employee_name, "employee_id": (e.id if e else None),
            "branch": a.branch, "sched_start": ss, "sched_end": se,
            "clock_in": _iso(a.clock_in_at), "clock_out": _iso(a.clock_out_at),
            "worked_minutes": worked, "late_minutes": late_min, "overtime_minutes": overtime,
            "status": a.status, "approval": a.approval,
            "lat": float(a.ci_lat) if a.ci_lat is not None else None,
            "lng": float(a.ci_lng) if a.ci_lng is not None else None,
            "distance": a.ci_dist, "source": a.source, "notes": a.reason,
        })
    return out


def _branch_att(b):
    return {"name": b.name, "lat": float(b.lat) if b.lat is not None else None,
            "lng": float(b.lng) if b.lng is not None else None,
            "radius_m": int(b.radius_m or 150), "timezone": b.timezone or "UTC",
            "loc_verify": b.loc_verify if b.loc_verify is not None else True,
            "grace_min": int(b.grace_min or 10),
            "allow_override": b.allow_override if b.allow_override is not None else True,
            "attendance_active": b.attendance_active if b.attendance_active is not None else True}


@router.get("/branch/{name}")
def get_branch_attendance(name: str, db: Session = Depends(get_db),
                          user: models.User = Depends(S.require("view"))):
    b = db.get(models.Branch, name)
    if not b:
        raise HTTPException(404, "Branch not found")
    return _branch_att(b)


@router.put("/branch/{name}")
def set_branch_attendance(name: str, body: dict, db: Session = Depends(get_db),
                          user: models.User = Depends(S.require("manage_branches"))):
    b = db.get(models.Branch, name)
    if not b:
        raise HTTPException(404, "Branch not found")
    for f in ("lat", "lng", "radius_m", "timezone", "loc_verify", "grace_min",
              "allow_override", "attendance_active"):
        if f in body and body[f] is not None:
            setattr(b, f, body[f])
    db.commit()
    S.audit(db, user, "update_branch_attendance", "branch", name,
            f"lat={b.lat} lng={b.lng} r={b.radius_m}")
    return _branch_att(b)
