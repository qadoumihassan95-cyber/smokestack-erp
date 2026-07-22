"""Team Chat — near-real-time internal messaging integrated with the ERP.

Transport: short polling (the host cannot run WebSockets). `GET /poll` returns
everything a client needs to refresh — new messages, unread counts, presence and
typing — in one round trip, so a 2-3s poll feels live without a socket.

Security: every room is membership- and RBAC-scoped. A user only ever sees rooms
they belong to, and branch rooms are auto-scoped to their branches. Permissions
come from the RBAC engine via security.require / permissions.can — never
hardcoded here.

Deferred (documented, not stubbed): file/photo/voice attachments need object
storage, which this host lacks. The message model has no binary column; when
storage exists an `attachments` table slots in without touching messaging.
"""
import json
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_

from ..database import get_db
from .. import models, security as S, permissions as P

router = APIRouter(prefix="/api/chat", tags=["chat"])

TYPING_WINDOW = 6          # seconds a "typing" flag stays live
ONLINE_WINDOW = 45         # seconds since last_seen to count as online


# ----------------------------------------------------------------- presence
def _touch(db, user):
    p = db.get(models.ChatPresence, user.id) or models.ChatPresence(user_id=user.id)
    p.last_seen = datetime.now(timezone.utc)
    db.merge(p)
    db.commit()


def _online(db):
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ONLINE_WINDOW)
    rows = db.query(models.ChatPresence).all()
    out = {}
    for p in rows:
        ls = p.last_seen
        if ls and ls.tzinfo is None:
            ls = ls.replace(tzinfo=timezone.utc)
        out[p.user_id] = {"online": bool(ls and ls >= cutoff),
                          "last_seen": (ls.isoformat() if ls else None)}
    return out


# ----------------------------------------------------------------- membership
def _profile(db, uid):
    u = db.get(models.User, uid)
    emp = db.query(models.Employee).filter(models.Employee.user_id == uid).first() \
        if uid else None
    if not emp and u:
        emp = db.query(models.Employee).filter(models.Employee.name == u.name).first()
    return {"id": uid, "name": (u.name if u else uid),
            "role": (u.role if u else None),
            "title": (emp.title if emp else None),
            "branch": (emp.branch if emp else None)}


def _rooms_for(db, user):
    """Every room this user may see: explicit memberships + auto branch/company."""
    ids = {m.room_id for m in db.query(models.ChatMember)
           .filter(models.ChatMember.user_id == user.id).all()}
    rooms = {r.id: r for r in db.query(models.ChatRoom)
             .filter(models.ChatRoom.archived == False).all()}  # noqa: E712
    brs = set(S.scope_branches(user, db))
    visible = []
    for r in rooms.values():
        if r.id in ids:
            visible.append(r)
        elif r.kind == "company" and P.can(user.role, "chat_company_room"):
            visible.append(r)
        elif r.kind == "branch" and r.branch in brs:
            visible.append(r)
    return visible


def _require_member(db, user, room_id):
    room = db.get(models.ChatRoom, room_id)
    if not room or room.archived:
        raise HTTPException(404, "Room not found")
    if room in _rooms_for(db, user):
        return room
    raise HTTPException(403, "You are not a member of this conversation.")


def _msg_out(db, m, reactions_by_msg=None):
    prof = _profile(db, m.user_id)
    react = (reactions_by_msg or {}).get(m.id, {})
    return {"id": m.id, "room_id": m.room_id, "user": prof,
            "body": ("" if m.deleted else m.body),
            "kind": m.kind, "deleted": bool(m.deleted), "edited": bool(m.edited),
            "pinned": bool(m.pinned), "reply_to": m.reply_to,
            "erp_ref": (json.loads(m.erp_ref) if m.erp_ref else None),
            "mentions": (json.loads(m.mentions) if m.mentions else []),
            "reactions": react,
            "at": (m.created_at.isoformat() if m.created_at else None)}


def _reactions(db, message_ids):
    if not message_ids:
        return {}
    rows = (db.query(models.ChatReaction)
            .filter(models.ChatReaction.message_id.in_(message_ids)).all())
    out = {}
    for r in rows:
        out.setdefault(r.message_id, {}).setdefault(r.emoji, []).append(r.user_id)
    return out


# --------------------------------------------------------------------- rooms
@router.get("/rooms")
def rooms(db: Session = Depends(get_db),
          user: models.User = Depends(S.require("chat_view"))):
    _touch(db, user)
    out = []
    for r in _rooms_for(db, user):
        last = (db.query(models.ChatMessage)
                .filter(models.ChatMessage.room_id == r.id)
                .order_by(models.ChatMessage.id.desc()).first())
        mem = (db.query(models.ChatMember)
               .filter(models.ChatMember.room_id == r.id,
                       models.ChatMember.user_id == user.id).first())
        last_read = mem.last_read_id if mem else 0
        unread = (db.query(models.ChatMessage)
                  .filter(models.ChatMessage.room_id == r.id,
                          models.ChatMessage.id > last_read,
                          models.ChatMessage.user_id != user.id).count())
        out.append({"id": r.id, "kind": r.kind,
                    "name": r.name or (f"{r.branch} branch" if r.branch else "Group"),
                    "branch": r.branch, "department": r.department,
                    "unread": unread,
                    "last": (_msg_out(db, last) if last else None)})
    out.sort(key=lambda x: (x["last"]["at"] if x["last"] else ""), reverse=True)
    return {"rooms": out}


@router.post("/rooms", status_code=201)
def create_room(body: dict, db: Session = Depends(get_db),
                user: models.User = Depends(S.require("chat_create_room"))):
    kind = (body or {}).get("kind", "group")
    if kind == "company" and not P.can(user.role, "chat_company_room"):
        raise HTTPException(403, "You may not create a company-wide room.")
    branch = (body or {}).get("branch")
    if branch:
        S.assert_branch(user, db, branch)
    r = models.ChatRoom(kind=kind, name=(body or {}).get("name"),
                        branch=branch, department=(body or {}).get("department"),
                        created_by=user.id)
    db.add(r); db.flush()
    members = set((body or {}).get("members") or [])
    members.add(user.id)
    for uid in members:
        if db.get(models.User, uid):
            db.add(models.ChatMember(room_id=r.id, user_id=uid,
                                     role=("admin" if uid == user.id else "member")))
    db.commit()
    S.audit(db, user, "chat_create_room", "chat_room", r.id,
            detail=f"{kind} · {r.name or branch or ''}")
    return {"id": r.id, "kind": r.kind, "name": r.name}


# ------------------------------------------------------------------ messages
@router.get("/rooms/{room_id}/messages")
def messages(room_id: int, before: int = 0, limit: int = 40,
             db: Session = Depends(get_db),
             user: models.User = Depends(S.require("chat_view"))):
    _require_member(db, user, room_id)
    _touch(db, user)
    q = db.query(models.ChatMessage).filter(models.ChatMessage.room_id == room_id)
    if before:
        q = q.filter(models.ChatMessage.id < before)
    rows = q.order_by(models.ChatMessage.id.desc()).limit(min(limit, 80)).all()
    rows = list(reversed(rows))
    react = _reactions(db, [m.id for m in rows])
    return {"messages": [_msg_out(db, m, react) for m in rows],
            "has_more": len(rows) == min(limit, 80)}


@router.post("/rooms/{room_id}/messages", status_code=201)
def send(room_id: int, body: dict, db: Session = Depends(get_db),
         user: models.User = Depends(S.require("chat_send"))):
    _require_member(db, user, room_id)
    text = (body or {}).get("body", "").strip()
    erp = (body or {}).get("erp_ref")
    if not text and not erp:
        raise HTTPException(422, "A message cannot be empty.")
    m = models.ChatMessage(
        room_id=room_id, user_id=user.id, body=text,
        kind=("erp_card" if erp else "text"),
        erp_ref=(json.dumps(erp) if erp else None),
        reply_to=(body or {}).get("reply_to"),
        mentions=json.dumps((body or {}).get("mentions") or []))
    db.add(m); db.flush()
    # sender has read their own message
    mem = (db.query(models.ChatMember)
           .filter(models.ChatMember.room_id == room_id,
                   models.ChatMember.user_id == user.id).first())
    if mem:
        mem.last_read_id = m.id
    _touch(db, user)
    db.commit()
    return _msg_out(db, m)


@router.patch("/messages/{mid}")
def edit(mid: int, body: dict, db: Session = Depends(get_db),
         user: models.User = Depends(S.require("chat_send"))):
    m = db.get(models.ChatMessage, mid)
    if not m or m.deleted:
        raise HTTPException(404, "Message not found")
    if m.user_id != user.id:
        raise HTTPException(403, "You can only edit your own messages.")
    _require_member(db, user, m.room_id)
    m.body = (body or {}).get("body", m.body)
    m.edited = True
    m.edited_at = datetime.now(timezone.utc)
    db.commit()
    return _msg_out(db, m)


@router.delete("/messages/{mid}")
def delete(mid: int, db: Session = Depends(get_db),
           user: models.User = Depends(S.require("chat_view"))):
    m = db.get(models.ChatMessage, mid)
    if not m:
        raise HTTPException(404, "Message not found")
    own = m.user_id == user.id
    if not own and not P.can(user.role, "chat_delete_message"):
        raise HTTPException(403, "You may not delete others' messages.")
    _require_member(db, user, m.room_id)
    m.deleted = True
    m.body = ""
    db.commit()
    S.audit(db, user, "chat_delete_message", "chat_message", mid,
            detail=("own" if own else "moderated"))
    return {"ok": True}


@router.post("/messages/{mid}/react")
def react(mid: int, body: dict, db: Session = Depends(get_db),
          user: models.User = Depends(S.require("chat_send"))):
    m = db.get(models.ChatMessage, mid)
    if not m:
        raise HTTPException(404, "Message not found")
    _require_member(db, user, m.room_id)
    emoji = (body or {}).get("emoji", "👍")[:8]
    existing = (db.query(models.ChatReaction)
                .filter(models.ChatReaction.message_id == mid,
                        models.ChatReaction.user_id == user.id,
                        models.ChatReaction.emoji == emoji).first())
    if existing:
        db.delete(existing)               # toggle off
    else:
        db.add(models.ChatReaction(message_id=mid, user_id=user.id, emoji=emoji))
    db.commit()
    return {"reactions": _reactions(db, [mid]).get(mid, {})}


@router.post("/messages/{mid}/pin")
def pin(mid: int, db: Session = Depends(get_db),
        user: models.User = Depends(S.require("chat_pin"))):
    m = db.get(models.ChatMessage, mid)
    if not m:
        raise HTTPException(404, "Message not found")
    _require_member(db, user, m.room_id)
    m.pinned = not m.pinned
    db.commit()
    S.audit(db, user, "chat_pin", "chat_message", mid, detail=str(m.pinned))
    return {"pinned": m.pinned}


# --------------------------------------------------------------- convert to action
@router.post("/messages/{mid}/to-task", status_code=201)
def to_task(mid: int, body: dict, db: Session = Depends(get_db),
            user: models.User = Depends(S.require("chat_create_task"))):
    m = db.get(models.ChatMessage, mid)
    if not m:
        raise HTTPException(404, "Message not found")
    _require_member(db, user, m.room_id)
    due = (body or {}).get("due_date")
    from datetime import date as _date
    due_d = None
    if due:
        try:
            due_d = _date.fromisoformat(due)
        except ValueError:
            raise HTTPException(422, "due_date must be YYYY-MM-DD")
    t = models.ChatTask(
        room_id=m.room_id, message_id=mid,
        title=((body or {}).get("title") or m.body or "Task")[:400],
        assignee=(body or {}).get("assignee") or user.id,
        priority=(body or {}).get("priority", "normal"),
        due_date=due_d, status="open", percent=0, created_by=user.id)
    db.add(t); db.flush()
    # a system message keeps the task visible in the conversation
    sysm = models.ChatMessage(room_id=m.room_id, user_id=user.id, kind="system",
                              body=f"Created task: {t.title}",
                              erp_ref=json.dumps({"type": "task", "id": t.id,
                                                  "label": t.title, "view": "chat"}))
    db.add(sysm)
    db.commit()
    S.audit(db, user, "chat_create_task", "chat_task", t.id, detail=t.title)
    return {"id": t.id, "title": t.title, "assignee": t.assignee,
            "priority": t.priority, "status": t.status}


@router.get("/tasks")
def tasks(room_id: int = 0, db: Session = Depends(get_db),
          user: models.User = Depends(S.require("chat_view"))):
    q = db.query(models.ChatTask)
    if room_id:
        _require_member(db, user, room_id)
        q = q.filter(models.ChatTask.room_id == room_id)
    else:
        mine = {r.id for r in _rooms_for(db, user)}
        q = q.filter(models.ChatTask.room_id.in_(mine or {0}))
    rows = q.order_by(models.ChatTask.id.desc()).limit(100).all()
    return {"tasks": [{"id": t.id, "room_id": t.room_id, "title": t.title,
                       "assignee": _profile(db, t.assignee), "priority": t.priority,
                       "due_date": str(t.due_date or ""), "status": t.status,
                       "percent": t.percent} for t in rows]}


@router.patch("/tasks/{tid}")
def update_task(tid: int, body: dict, db: Session = Depends(get_db),
                user: models.User = Depends(S.require("chat_create_task"))):
    t = db.get(models.ChatTask, tid)
    if not t:
        raise HTTPException(404, "Task not found")
    _require_member(db, user, t.room_id)
    if "status" in body:
        t.status = body["status"]
    if "percent" in body:
        t.percent = max(0, min(100, int(body["percent"])))
        if t.percent == 100:
            t.status = "done"
    db.add(models.ChatMessage(room_id=t.room_id, user_id=user.id, kind="system",
                              body=f"Task “{t.title}” → {t.status} ({t.percent}%)"))
    db.commit()
    return {"id": t.id, "status": t.status, "percent": t.percent}


# ---------------------------------------------------------------- announcements
@router.get("/announcements")
def announcements(db: Session = Depends(get_db),
                  user: models.User = Depends(S.require("chat_view"))):
    brs = set(S.scope_branches(user, db))
    rows = (db.query(models.ChatAnnouncement)
            .filter(models.ChatAnnouncement.active == True)  # noqa: E712
            .order_by(models.ChatAnnouncement.id.desc()).all())
    out = [a for a in rows if a.scope == "company" or a.branch in brs]
    return {"announcements": [{"id": a.id, "scope": a.scope, "branch": a.branch,
                               "title": a.title, "body": a.body,
                               "by": _profile(db, a.created_by),
                               "at": (a.created_at.isoformat() if a.created_at else None)}
                              for a in out]}


@router.post("/announcements", status_code=201)
def announce(body: dict, db: Session = Depends(get_db),
             user: models.User = Depends(S.require("chat_announce"))):
    scope = (body or {}).get("scope", "company")
    branch = (body or {}).get("branch")
    if scope == "company" and not P.can(user.role, "chat_company_room"):
        raise HTTPException(403, "You may not post a company-wide announcement.")
    if branch:
        S.assert_branch(user, db, branch)
    a = models.ChatAnnouncement(scope=scope, branch=branch,
                                title=(body or {}).get("title", "").strip(),
                                body=(body or {}).get("body", "").strip(),
                                created_by=user.id)
    if not a.title:
        raise HTTPException(422, "An announcement needs a title.")
    db.add(a); db.commit()
    S.audit(db, user, "chat_announce", "announcement", a.id, detail=a.title)
    return {"id": a.id, "title": a.title}


@router.delete("/announcements/{aid}")
def unannounce(aid: int, db: Session = Depends(get_db),
               user: models.User = Depends(S.require("chat_announce"))):
    a = db.get(models.ChatAnnouncement, aid)
    if not a:
        raise HTTPException(404, "Not found")
    a.active = False
    db.commit()
    S.audit(db, user, "chat_unannounce", "announcement", aid)
    return {"ok": True}


# ------------------------------------------------------------------ ERP sharing
@router.get("/erp-search")
def erp_search(q: str = "", db: Session = Depends(get_db),
               user: models.User = Depends(S.require("chat_send"))):
    """Find an ERP record to attach as a card. Reuses the assistant's scoped
    search so a user can only share records they are allowed to see."""
    from ..assistant import tools as T
    try:
        res = T.run("search.global", db, user, q=q)
    except (T.Denied, T.ToolError):
        return {"results": []}
    flat = []
    for g in res.get("groups", []):
        for row in g["rows"]:
            flat.append({"type": g["group"][:-1] if g["group"].endswith("s") else g["group"],
                         "id": row["ref"], "label": row["title"],
                         "subtitle": row["subtitle"], "view": row["view"]})
    return {"results": flat[:20]}


# ------------------------------------------------------------------ typing
@router.post("/rooms/{room_id}/typing")
def typing(room_id: int, db: Session = Depends(get_db),
           user: models.User = Depends(S.require("chat_send"))):
    _require_member(db, user, room_id)
    p = db.get(models.ChatPresence, user.id) or models.ChatPresence(user_id=user.id)
    p.typing_room = room_id
    p.typing_at = datetime.now(timezone.utc)
    p.last_seen = datetime.now(timezone.utc)
    db.merge(p); db.commit()
    return {"ok": True}


@router.post("/rooms/{room_id}/read")
def mark_read(room_id: int, body: dict, db: Session = Depends(get_db),
              user: models.User = Depends(S.require("chat_view"))):
    _require_member(db, user, room_id)
    mem = (db.query(models.ChatMember)
           .filter(models.ChatMember.room_id == room_id,
                   models.ChatMember.user_id == user.id).first())
    if not mem:      # auto-membership (branch/company) — create on first read
        mem = models.ChatMember(room_id=room_id, user_id=user.id)
        db.add(mem)
    mem.last_read_id = max(mem.last_read_id or 0, int((body or {}).get("up_to") or 0))
    db.commit()
    return {"ok": True, "last_read_id": mem.last_read_id}


# ------------------------------------------------------------------ the poll
@router.get("/poll")
def poll(room_id: int = 0, after: int = 0, db: Session = Depends(get_db),
         user: models.User = Depends(S.require("chat_view"))):
    """One round trip: new messages in the open room, presence, who is typing,
    and per-room unread totals. The client calls this every 2-3 seconds."""
    _touch(db, user)
    new_msgs = []
    typing = []
    if room_id:
        _require_member(db, user, room_id)
        rows = (db.query(models.ChatMessage)
                .filter(models.ChatMessage.room_id == room_id,
                        models.ChatMessage.id > after)
                .order_by(models.ChatMessage.id.asc()).limit(60).all())
        react = _reactions(db, [m.id for m in rows])
        new_msgs = [_msg_out(db, m, react) for m in rows]
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=TYPING_WINDOW)
        for p in db.query(models.ChatPresence).filter(
                models.ChatPresence.typing_room == room_id).all():
            ta = p.typing_at
            if ta and ta.tzinfo is None:
                ta = ta.replace(tzinfo=timezone.utc)
            if p.user_id != user.id and ta and ta >= cutoff:
                typing.append(_profile(db, p.user_id))
    # unread per room
    unread = {}
    for r in _rooms_for(db, user):
        mem = (db.query(models.ChatMember)
               .filter(models.ChatMember.room_id == r.id,
                       models.ChatMember.user_id == user.id).first())
        lr = mem.last_read_id if mem else 0
        unread[r.id] = (db.query(models.ChatMessage)
                        .filter(models.ChatMessage.room_id == r.id,
                                models.ChatMessage.id > lr,
                                models.ChatMessage.user_id != user.id).count())
    return {"messages": new_msgs, "typing": typing,
            "presence": _online(db), "unread": unread,
            "total_unread": sum(unread.values())}


# ------------------------------------------------------------------ smart alerts
def post_system_alert(db, kind, text, branch=None, erp_ref=None):
    """Called by other modules to drop a business alert into the right rooms.

    Routing: branch alerts go to that branch's room; everything else to the
    company room. Silently does nothing if no suitable room exists yet, so a
    caller never has to guard against chat being unconfigured.
    """
    q = db.query(models.ChatRoom).filter(models.ChatRoom.archived == False)  # noqa: E712
    room = None
    if branch:
        room = q.filter(models.ChatRoom.kind == "branch",
                        models.ChatRoom.branch == branch).first()
    if not room:
        room = q.filter(models.ChatRoom.kind == "company").first()
    if not room:
        return None
    import json as _json
    m = models.ChatMessage(room_id=room.id, user_id="system", kind="alert",
                           body=text,
                           erp_ref=(_json.dumps(erp_ref) if erp_ref else None))
    db.add(m); db.commit()
    return m.id


@router.post("/alerts/test", status_code=201)
def alert_test(body: dict, db: Session = Depends(get_db),
               user: models.User = Depends(S.require("chat_announce"))):
    """Admin-triggered alert, for verifying routing without waiting for an event."""
    mid = post_system_alert(db, (body or {}).get("kind", "test"),
                            (body or {}).get("text", "Test alert"),
                            branch=(body or {}).get("branch"))
    if not mid:
        raise HTTPException(422, "No company or branch room exists to post into yet.")
    S.audit(db, user, "chat_alert", "chat_message", mid, detail=(body or {}).get("kind"))
    return {"message_id": mid}
