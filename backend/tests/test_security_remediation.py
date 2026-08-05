"""Phase-1 security remediation tests.

Covers the two confirmed audit findings:
  * H1 — GET /api/telegram/session/{tg_id} is now bot-only, fail-closed,
    constant-time, and leaks nothing (identity, role, branch, or existence)
    to an unauthenticated caller.
  * C1 — the default-account remediation tool's guardrails: fail-closed on a
    missing/invalid replacement owner, refuses to remove the last active owner,
    is idempotent, audits every action, and never exposes credentials.
"""
import os
import tempfile
from types import SimpleNamespace

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_secrem_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "test-secret"

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.config import settings  # noqa: E402
from scripts import remediate_default_accounts as R  # noqa: E402

client = TestClient(app)
BOT_SECRET = "TESTBOT-SECRET"


def _tok(uid="U-owner"):
    r = client.post("/api/auth/login", data={"username": uid, "password": "demo1234"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _free_slot(user_id=None, tg_id=None):
    from app.database import SessionLocal
    from app import models as _m
    db = SessionLocal()
    try:
        q = db.query(_m.TelegramLink)
        rows = list(q.filter(_m.TelegramLink.tg_id == str(tg_id)).all()) if tg_id else []
        if user_id:
            rows += list(q.filter(_m.TelegramLink.user_id == user_id,
                                  _m.TelegramLink.status == "active").all())
        for r in rows:
            db.delete(r)
        db.commit()
    finally:
        db.close()


def _link(tg_id, username="hassan"):
    h = _tok()
    _free_slot(user_id="U-owner", tg_id=tg_id)
    code = client.post("/api/telegram/link-code", headers=h).json()["code"]
    r = client.post("/api/telegram/link/verify",
                    json={"tg_id": tg_id, "code": code, "username": username, "device": "Telegram"})
    assert r.status_code == 200, r.text


# ============================================================ H1: session auth
def test_session_missing_token_is_forbidden_and_leaks_nothing():
    settings.bot_token = BOT_SECRET
    with TestClient(app):
        _link("900100")
        r = client.get("/api/telegram/session/900100")  # no header
        assert r.status_code == 403
        body = r.text
        # None of the sensitive fields may appear anywhere in the response.
        for leak in ("linked", "role", "owner", "branches", "username", "U-owner"):
            assert leak not in body


def test_session_invalid_token_is_forbidden():
    settings.bot_token = BOT_SECRET
    with TestClient(app):
        _link("900101")
        r = client.get("/api/telegram/session/900101", headers={"X-Bot-Token": "wrong"})
        assert r.status_code == 403
        assert "role" not in r.text and "owner" not in r.text


def test_session_unset_server_token_fails_closed():
    settings.bot_token = ""  # secret not configured on the server
    with TestClient(app):
        # Even presenting *some* token must be rejected when the server has none.
        r = client.get("/api/telegram/session/900100", headers={"X-Bot-Token": "anything"})
        assert r.status_code == 403


def test_session_valid_token_linked_returns_profile():
    settings.bot_token = BOT_SECRET
    BOT = {"X-Bot-Token": BOT_SECRET}
    with TestClient(app):
        _link("900102", username="hassan")
        s = client.get("/api/telegram/session/900102", headers=BOT).json()
        assert s["linked"] is True
        assert s["user"]["role"] == "owner" and s["user"]["name"]
        assert s["status"] == "connected"


def test_session_valid_token_unlinked_returns_false():
    settings.bot_token = BOT_SECRET
    BOT = {"X-Bot-Token": BOT_SECRET}
    with TestClient(app):
        s = client.get("/api/telegram/session/404404", headers=BOT).json()
        assert s == {"linked": False}


def test_session_anonymous_cannot_enumerate_existence():
    """A linked vs a non-existent tg_id must be indistinguishable to an anonymous
    caller — both a bare 403, so existence is never revealed."""
    settings.bot_token = BOT_SECRET
    with TestClient(app):
        _link("900103")
        existing = client.get("/api/telegram/session/900103")       # linked, no auth
        missing = client.get("/api/telegram/session/555555")        # not linked, no auth
        assert existing.status_code == missing.status_code == 403
        assert existing.text == missing.text  # identical response → no signal


def test_session_anonymous_cannot_read_identity_role_or_branches():
    settings.bot_token = BOT_SECRET
    with TestClient(app):
        _link("900104")
        r = client.get("/api/telegram/session/900104")
        assert r.status_code == 403
        data = r.json()
        assert "user" not in data and "role" not in data and "branches" not in data


# ============================================================ C1: remediation guards
def _u(uid, role, status="active"):
    return SimpleNamespace(id=uid, role=role, status=status, can_login=True,
                           must_change_password=False, password_hash="x")


def test_verify_replacement_fail_closed_cases():
    import pytest
    users = {"U-owner": _u("U-owner", "owner"),
             "U-realowner": _u("U-realowner", "owner"),
             "U-someadmin": _u("U-someadmin", "admin"),
             "U-inactive": _u("U-inactive", "owner", status="disabled")}
    # missing arg
    with pytest.raises(ValueError):
        R.verify_replacement(users, None)
    # a default seeded id is not an acceptable replacement
    with pytest.raises(ValueError):
        R.verify_replacement(users, "U-owner")
    # not found
    with pytest.raises(ValueError):
        R.verify_replacement(users, "U-ghost")
    # wrong role
    with pytest.raises(ValueError):
        R.verify_replacement(users, "U-someadmin")
    # inactive
    with pytest.raises(ValueError):
        R.verify_replacement(users, "U-inactive")
    # valid
    assert R.verify_replacement(users, "U-realowner").id == "U-realowner"


def test_assert_owner_survives_refuses_last_owner():
    import pytest
    # Only owner is the default U-owner; disabling it would orphan ownership.
    users = {"U-owner": _u("U-owner", "owner"), "U-emp": _u("U-emp", "employee")}
    with pytest.raises(ValueError):
        R.assert_owner_survives(users, ["U-owner"], verified_owner_id="U-owner")
    # With a real replacement owner present, disabling the default owner is allowed.
    users["U-realowner"] = _u("U-realowner", "owner")
    remaining = R.assert_owner_survives(users, ["U-owner"], verified_owner_id="U-realowner")
    assert "U-realowner" in remaining and "U-owner" not in remaining


_iso_ctr = iter(range(1, 10_000))


def _isolated_seeded_session():
    """A fully independent SQLite DB (its own engine) seeded with the default
    U-* accounts plus a real replacement owner. Kept OUT of the process-shared
    app engine so remediation mutations here never affect other test modules."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    from app import models, security as S, seed as seedmod
    try:
        from app import tenancy
    except Exception:  # noqa: BLE001
        tenancy = None
    path = os.path.join(tempfile.gettempdir(), f"secrem_iso_{os.getpid()}_{next(_iso_ctr)}.db")
    if os.path.exists(path):
        os.remove(path)
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    db = sessionmaker(bind=eng)()
    if tenancy is not None:
        try:
            tenancy.use_system_context(db)  # privileged maintenance context
        except Exception:  # noqa: BLE001
            pass
    seedmod.seed(db)  # branches + the seven default U-* accounts
    db.add(models.User(id="U-realowner", name="Real Owner", role="owner",
                       email="real@x.local", password_hash=S.hash_pw("x-not-secret"),
                       status="active"))
    db.commit()
    return db


def test_remediate_dry_run_changes_nothing():
    from app import models
    db = _isolated_seeded_session()
    try:
        res = R.remediate(db, verified_owner_id="U-realowner", mode="disable", apply=False)
        assert res["applied"] is False and res["actions"]
        assert (db.get(models.User, "U-owner").status or "active") == "active"  # untouched
    finally:
        db.close()


def test_remediate_apply_disables_defaults_and_audits():
    from app import models
    db = _isolated_seeded_session()
    try:
        res = R.remediate(db, verified_owner_id="U-realowner", mode="disable", apply=True)
        assert res["applied"] is True
        for aid in R.DEFAULT_ACCOUNT_IDS:  # every default now disabled
            u = db.get(models.User, aid)
            if u:
                assert u.status == "disabled" and u.can_login is False
        assert db.get(models.User, "U-realowner").status == "active"  # real owner survives
        rows = db.query(models.AuditLog).filter(models.AuditLog.source == "SECURITY").all()
        assert rows, "expected SECURITY audit rows"
        for r in rows:
            assert r.action.startswith("security.")
            assert "demo1234" not in (r.detail or "")
            assert "password_hash" not in (r.detail or "")
    finally:
        db.close()


def test_remediate_refuses_without_verified_owner():
    import pytest
    db = _isolated_seeded_session()
    try:
        with pytest.raises(ValueError):
            R.remediate(db, verified_owner_id=None, mode="disable", apply=False)
    finally:
        db.close()


def test_force_reset_invalidates_default_password():
    """force-reset rotates the known default password to an undisclosed secret and
    flags must_change_password — so 'demo1234' can no longer authenticate."""
    from app import models, security as S
    db = _isolated_seeded_session()
    try:
        R.remediate(db, verified_owner_id="U-realowner", mode="force-reset", apply=True)
        u = db.get(models.User, "U-cash")
        assert u.must_change_password is True
        assert S.verify_pw("demo1234", u.password_hash) is False  # default no longer works
    finally:
        db.close()
