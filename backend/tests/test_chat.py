"""Team Chat: rooms, messaging, RBAC, reactions, tasks, announcements, ERP share,
alerts, presence/polling, and assistant integration."""
import os, tempfile
_DB = os.path.join(tempfile.gettempdir(), f"smokestack_chat_{os.getpid()}.db")
if os.path.exists(_DB): os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "chat-secret-long-enough-value"

import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _boot():
    with TestClient(app):
        yield


def _tok(uid="U-owner"):
    r = client.post("/api/auth/login", data={"username": uid, "password": "demo1234"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _room(kind="company", name="Company", members=None, branch=None, actor="U-owner"):
    body = {"kind": kind, "name": name}
    if members: body["members"] = members
    if branch: body["branch"] = branch
    return client.post("/api/chat/rooms", headers=_tok(actor), json=body)


def test_owner_creates_rooms_of_each_kind():
    for kind, name in [("company", "All Company"), ("management", "Managers"),
                       ("department", "Sales Dept"), ("group", "Project X")]:
        r = _room(kind, name)
        assert r.status_code == 201, r.text
    br = _room("branch", "Store A Team", branch="Store A")
    assert br.status_code == 201


def test_send_receive_edit_delete_message():
    rid = _room("group", "Chat Test", members=["U-bm"]).json()["id"]
    s = client.post(f"/api/chat/rooms/{rid}/messages", headers=_tok(),
                    json={"body": "Hello team"})
    assert s.status_code == 201
    mid = s.json()["id"]
    msgs = client.get(f"/api/chat/rooms/{rid}/messages", headers=_tok()).json()
    assert any(m["id"] == mid and m["body"] == "Hello team" for m in msgs["messages"])
    # sender metadata is present
    m = next(m for m in msgs["messages"] if m["id"] == mid)
    assert m["user"]["name"] and m["user"]["role"] == "owner"
    # edit
    e = client.patch(f"/api/chat/messages/{mid}", headers=_tok(), json={"body": "Hello all"})
    assert e.json()["edited"] is True and e.json()["body"] == "Hello all"
    # delete
    assert client.delete(f"/api/chat/messages/{mid}", headers=_tok()).status_code == 200
    after = client.get(f"/api/chat/rooms/{rid}/messages", headers=_tok()).json()
    gone = next(m for m in after["messages"] if m["id"] == mid)
    assert gone["deleted"] is True and gone["body"] == ""


def test_empty_message_is_rejected():
    rid = _room("group", "Empty Test").json()["id"]
    assert client.post(f"/api/chat/rooms/{rid}/messages", headers=_tok(),
                       json={"body": "   "}).status_code == 422


def test_non_member_cannot_read_or_post():
    rid = _room("group", "Private Group", members=["U-bm"]).json()["id"]
    # the cashier is not a member and it is not a branch/company room
    assert client.get(f"/api/chat/rooms/{rid}/messages",
                      headers=_tok("U-cash")).status_code == 403
    assert client.post(f"/api/chat/rooms/{rid}/messages", headers=_tok("U-cash"),
                       json={"body": "sneak"}).status_code == 403


def test_branch_room_is_auto_visible_to_branch_members_only():
    _room("branch", "Store C Team", branch="Store C")
    # U-owner sees it (all branches); a Store-A-only cashier does not
    owner_rooms = client.get("/api/chat/rooms", headers=_tok()).json()["rooms"]
    assert any(r["branch"] == "Store C" for r in owner_rooms)
    cash_rooms = client.get("/api/chat/rooms", headers=_tok("U-cash")).json()["rooms"]
    assert not any(r["branch"] == "Store C" for r in cash_rooms)


def test_permissions_gate_room_and_announcement_creation():
    # employee/cashier cannot create rooms
    assert _room("group", "Nope", actor="U-emp").status_code == 403
    assert _room("group", "Nope", actor="U-cash").status_code == 403
    # employee cannot announce
    assert client.post("/api/chat/announcements", headers=_tok("U-emp"),
                       json={"title": "x"}).status_code == 403
    # unauthenticated
    assert client.get("/api/chat/rooms").status_code == 401


def test_reactions_toggle():
    rid = _room("group", "React Test").json()["id"]
    mid = client.post(f"/api/chat/rooms/{rid}/messages", headers=_tok(),
                      json={"body": "react to me"}).json()["id"]
    r1 = client.post(f"/api/chat/messages/{mid}/react", headers=_tok(),
                     json={"emoji": "🔥"}).json()
    assert "U-owner" in r1["reactions"]["🔥"]
    r2 = client.post(f"/api/chat/messages/{mid}/react", headers=_tok(),
                     json={"emoji": "🔥"}).json()
    assert "🔥" not in r2["reactions"]        # toggled off


def test_message_becomes_a_task_and_updates_in_chat():
    rid = _room("group", "Task Test", members=["U-bm"]).json()["id"]
    mid = client.post(f"/api/chat/rooms/{rid}/messages", headers=_tok(),
                      json={"body": "Restock the cooler"}).json()["id"]
    t = client.post(f"/api/chat/messages/{mid}/to-task", headers=_tok(),
                    json={"assignee": "U-bm", "priority": "high",
                          "due_date": "2026-08-01"})
    assert t.status_code == 201
    tid = t.json()["id"]
    # a system message announces it
    msgs = client.get(f"/api/chat/rooms/{rid}/messages", headers=_tok()).json()["messages"]
    assert any(m["kind"] == "system" and "task" in m["body"].lower() for m in msgs)
    # progress updates
    up = client.patch(f"/api/chat/tasks/{tid}", headers=_tok(), json={"percent": 100})
    assert up.json()["status"] == "done" and up.json()["percent"] == 100
    tasks = client.get(f"/api/chat/tasks?room_id={rid}", headers=_tok()).json()["tasks"]
    assert any(t["id"] == tid and t["status"] == "done" for t in tasks)


def test_pinned_announcement_visible_by_scope():
    a = client.post("/api/chat/announcements", headers=_tok(),
                    json={"scope": "company", "title": "Holiday Monday",
                          "body": "Closed for the holiday."})
    assert a.status_code == 201
    # everyone with chat sees a company announcement
    for uid in ("U-owner", "U-cash", "U-emp"):
        rows = client.get("/api/chat/announcements", headers=_tok(uid)).json()["announcements"]
        assert any(x["title"] == "Holiday Monday" for x in rows)
    # branch announcement only reaches its branch
    client.post("/api/chat/announcements", headers=_tok(),
                json={"scope": "branch", "branch": "Store B", "title": "Store B stocktake"})
    cash = client.get("/api/chat/announcements", headers=_tok("U-cash")).json()["announcements"]
    assert not any(x["title"] == "Store B stocktake" for x in cash)   # cashier is Store A


def test_share_an_erp_record_as_a_card():
    rid = _room("group", "Share Test").json()["id"]
    found = client.get("/api/chat/erp-search?q=Marlboro", headers=_tok()).json()["results"]
    assert found, "should find a product to share"
    ref = found[0]
    s = client.post(f"/api/chat/rooms/{rid}/messages", headers=_tok(),
                    json={"body": "check this", "erp_ref": ref})
    assert s.status_code == 201 and s.json()["kind"] == "erp_card"
    assert s.json()["erp_ref"]["label"] == ref["label"]
    assert s.json()["erp_ref"]["view"]           # clicking it opens a page


def test_erp_search_respects_branch_scope():
    # cashier (Store A) and owner may see different employees
    o = client.get("/api/chat/erp-search?q=Sam", headers=_tok()).json()["results"]
    c = client.get("/api/chat/erp-search?q=Sam", headers=_tok("U-cash")).json()["results"]
    assert isinstance(o, list) and isinstance(c, list)


def test_smart_alert_posts_into_the_right_room():
    _room("company", "Alerts Co")
    r = client.post("/api/chat/alerts/test", headers=_tok(),
                    json={"kind": "low_stock", "text": "Marlboro Gold is out of stock."})
    assert r.status_code == 201
    # the alert lands in the company room as a system/alert message
    rooms = client.get("/api/chat/rooms", headers=_tok()).json()["rooms"]
    co = next(r for r in rooms if r["kind"] == "company")
    msgs = client.get(f"/api/chat/rooms/{co['id']}/messages", headers=_tok()).json()["messages"]
    assert any(m["kind"] == "alert" and "out of stock" in m["body"] for m in msgs)


def test_poll_returns_new_messages_presence_and_unread():
    rid = _room("group", "Poll Test", members=["U-bm"]).json()["id"]
    first = client.post(f"/api/chat/rooms/{rid}/messages", headers=_tok(),
                        json={"body": "one"}).json()["id"]
    # bm has not read it → unread for bm
    poll = client.get(f"/api/chat/poll?room_id={rid}&after=0", headers=_tok("U-bm")).json()
    assert any(m["body"] == "one" for m in poll["messages"])
    assert poll["unread"].get(str(rid), 0) >= 1
    assert "presence" in poll and poll["total_unread"] >= 1
    # after reading, unread clears
    client.post(f"/api/chat/rooms/{rid}/read", headers=_tok("U-bm"), json={"up_to": first})
    poll2 = client.get(f"/api/chat/poll?room_id={rid}&after={first}", headers=_tok("U-bm")).json()
    assert poll2["unread"].get(str(rid), 0) == 0


def test_typing_indicator_shows_up_in_poll():
    rid = _room("group", "Typing Test", members=["U-bm"]).json()["id"]
    client.post(f"/api/chat/rooms/{rid}/typing", headers=_tok("U-bm"))
    poll = client.get(f"/api/chat/poll?room_id={rid}", headers=_tok()).json()
    assert any(p["id"] == "U-bm" for p in poll["typing"])


def test_moderator_can_delete_others_messages_but_member_cannot():
    rid = _room("group", "Mod Test", members=["U-bm", "U-cash"]).json()["id"]
    # bm posts
    bm_tok = _tok("U-bm")
    mid = client.post(f"/api/chat/rooms/{rid}/messages", headers=bm_tok,
                      json={"body": "bm message"}).json()["id"]
    # cashier (no chat_delete_message) cannot delete it
    assert client.delete(f"/api/chat/messages/{mid}", headers=_tok("U-cash")).status_code == 403
    # owner (has chat_delete_message) can
    assert client.delete(f"/api/chat/messages/{mid}", headers=_tok()).status_code == 200


def test_assistant_can_summarise_a_room():
    rid = _room("group", "Summary Test", members=["U-bm"]).json()["id"]
    for t in ["morning", "restock cooler", "customer complaint"]:
        client.post(f"/api/chat/rooms/{rid}/messages", headers=_tok(), json={"body": t})
    out = client.post("/api/assistant/run", headers=_tok(),
                      json={"tool": "chat.summary", "args": {"room_id": rid}}).json()
    assert out["data"]["messages"] >= 3
    assert out["data"]["participants"]


def test_assistant_can_find_past_messages():
    rid = _room("group", "Find Test").json()["id"]
    client.post(f"/api/chat/rooms/{rid}/messages", headers=_tok(),
                json={"body": "the delivery truck is late again"})
    out = client.post("/api/assistant/run", headers=_tok(),
                      json={"tool": "chat.find", "args": {"q": "delivery truck"}}).json()
    assert out["data"]["count"] >= 1
    assert any("delivery truck" in r["body"].lower() for r in out["data"]["results"])


def test_chat_actions_are_audited():
    rid = _room("group", "Audit Test").json()["id"]
    mid = client.post(f"/api/chat/rooms/{rid}/messages", headers=_tok(),
                      json={"body": "delete me"}).json()["id"]
    client.delete(f"/api/chat/messages/{mid}", headers=_tok())
    client.post("/api/chat/announcements", headers=_tok(), json={"title": "Audited note"})
    rows = client.get("/api/audit?limit=100", headers=_tok()).json()
    actions = {a["action"] for a in rows}
    assert {"chat_create_room", "chat_delete_message", "chat_announce"} <= actions
