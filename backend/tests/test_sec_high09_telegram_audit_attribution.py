"""SEC HIGH-09 regression — `POST /api/telegram/audit` must not let its caller choose
who an audit row is attributed to, or which tenant it lands in.

At `3eb1ccd` every attribution field of the written `AuditLog` came straight from the
request body — `user_id`, `role`, `branch`, `tg_username`, `ip` — with the shared bot
token as the only control. So any holder of that token could write audit history naming
any user, in any role, at any branch, from any IP. And because the endpoint is
unauthenticated, the session carried no company context, `_stamp_writes` did not stamp
it, and the row took the `company_id` server default of 1 — another tenant's Telegram
audit landing in company 1.

THE PROPERTY IS AUTHENTICITY, NOT TAMPER-RESISTANCE. The auditor's original gate marked
audit integrity PASS on evidence that rows cannot be erased or altered. Proving a row
cannot be ERASED is worth nothing if a row can be MANUFACTURED: a fabricated row is
indistinguishable from a genuine one, and a user cannot erase their history but history
can be built around them. That is why this module attacks the write and not the delete.

THE SPLIT THE FIX MAKES. Content — what happened (`action`, `entity`, `ref`, `detail`,
`result`) — is the bot's to report. Attribution — who did it, as what, where, from
where, for which tenant — is read from the server's own records via the `tg_id`'s link.

HONEST LIMIT, ASSERTED AS SUCH IN TEST 5. This does not make the endpoint unforgeable.
The bot token is one shared secret, so whoever holds it can still attribute an action to
any tg_id that is ACTUALLY LINKED. That is inherent to "the bot asserts what its users
did" and closing it needs per-user signing — a design change, not a patch. What is
closed is fabricating identities, roles, branches and tenants that do not exist.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"sec_h09_tgaudit_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("SEED_ON_START", "true")
os.environ.setdefault("SEED_DEMO_DATA", "true")
os.environ.setdefault("JWT_SECRET", "sec-h09-secret-long-enough-for-tests")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")

from fastapi.testclient import TestClient            # noqa: E402

from app.main import app                             # noqa: E402
from app.config import settings                      # noqa: E402
from app import models, security, tenancy            # noqa: E402

with TestClient(app):
    pass
client = TestClient(app)

def _bot():
    """Read the token from settings AT CALL TIME.

    `tests/test_telegram.py` sets `settings.bot_token = "TESTBOT"` globally and never
    restores it, so a hardcoded header here is correct or not depending on module
    ordering — this module happens to sort before it today. Ordering is not a
    property to depend on.
    """
    return {"X-Bot-Token": settings.bot_token}
C2 = 2
C2_USER = "H9-c2-user"
C2_TG = "900000002"
C1_TG = "900000001"
C1_USER = "U-cash"          # a real company-1 cashier, Store A


def setup_module(_m):
    """One linked Telegram account in company 1 and one in company 2, so the
    tenant the row lands in is a real question and not company 1 by default."""
    with tenancy.system_session() as db:
        if not db.query(models.Company).filter(models.Company.id == C2).first():
            db.add(models.Company(id=C2, name="Tango Co", slug="tango-co-h9",
                                  application_key="smoke_shop",
                                  owner_user_id=C2_USER, status="active"))
        db.commit()
    with tenancy.tenant_session(C2) as db:
        if not db.get(models.User, C2_USER):
            db.add(models.User(id=C2_USER, name="Tango Staff", role="employee",
                               password_hash=security.hash_pw("Tango-Pass-4417"),
                               status="active", can_login=True))
        if not db.query(models.Branch).filter(models.Branch.name == "Tango Depot").first():
            db.add(models.Branch(name="Tango Depot", timezone="UTC"))
        db.commit()
    with tenancy.system_session() as db:
        for tg, uid, cid, uname in ((C1_TG, C1_USER, 1, "c1_cashier"),
                                    (C2_TG, C2_USER, C2, "c2_staff")):
            if not db.get(models.TelegramLink, tg):
                link = models.TelegramLink(tg_id=tg, user_id=uid, username=uname,
                                           status="active")
                link.company_id = cid
                db.add(link)
        db.commit()


def teardown_module(_m):
    with tenancy.system_session() as db:
        db.query(models.AuditLog).filter(models.AuditLog.source == "TELEGRAM",
                                         models.AuditLog.tg_id.in_([C1_TG, C2_TG])).delete(
            synchronize_session=False)
        db.query(models.TelegramLink).filter(
            models.TelegramLink.tg_id.in_([C1_TG, C2_TG])).delete(synchronize_session=False)
        # One process-wide database: a branch left behind here appears in other
        # modules' branch-label and branch-count assertions.
        db.query(models.Branch).filter(models.Branch.name == "Tango Depot").delete(
            synchronize_session=False)
        db.commit()


def _latest(ref):
    with tenancy.system_session() as db:
        return (db.query(models.AuditLog)
                .filter(models.AuditLog.ref == ref)
                .order_by(models.AuditLog.id.desc()).first())


def _count():
    with tenancy.system_session() as db:
        return db.query(models.AuditLog).count()


# ===========================================================================
# 0. CONTROLS.
# ===========================================================================
def test_0_a_legitimate_bot_call_still_writes_an_audit_row():
    """If the endpoint simply stopped working, every refusal below would pass
    while the bot's real audit trail silently disappeared."""
    r = client.post("/api/telegram/audit",
                    json={"tg_id": C1_TG, "action": "clock_in", "entity": "attendance",
                          "ref": "H9-CONTROL-1", "detail": "control row", "result": "ok"},
                    headers=_bot())
    assert r.status_code == 200, r.text
    row = _latest("H9-CONTROL-1")
    assert row is not None, "the legitimate call wrote nothing"
    assert row.action == "clock_in" and row.entity == "attendance", (
        "content the bot reported was not preserved")
    assert row.source == "TELEGRAM"


def test_1_the_bot_token_is_still_required():
    for headers in ({}, {"X-Bot-Token": "wrong-token"}, {"X-Bot-Token": ""}):
        r = client.post("/api/telegram/audit",
                        json={"tg_id": C1_TG, "action": "x", "ref": "H9-NOTOKEN"},
                        headers=headers)
        assert r.status_code == 403, f"headers={headers}: got {r.status_code}"
    assert _latest("H9-NOTOKEN") is None


# ===========================================================================
# 1. THE FINDING — attribution is server-derived, never caller-supplied.
# ===========================================================================
def test_2_caller_supplied_attribution_is_ignored_in_favour_of_the_link():
    """The full forgery from the report: a caller naming a different user, a
    different role, a different branch and a different IP."""
    r = client.post("/api/telegram/audit",
                    json={"tg_id": C1_TG,
                          # --- attribution the attacker tries to choose ---
                          "user_id": "U-owner", "role": "owner",
                          "tg_username": "impersonated_owner", "ip": "203.0.113.99",
                          # `branch` is deliberately IN scope here (U-cash holds Store A)
                          # so this test measures the other four fields. The
                          # out-of-scope branch is test 2b.
                          "branch": "Store A",
                          # --- content, which is legitimately theirs to report ---
                          "action": "approve", "entity": "purchase", "ref": "H9-FORGE-1",
                          "detail": "approved by owner", "result": "ok"},
                    headers=_bot())
    assert r.status_code == 200, r.text

    row = _latest("H9-FORGE-1")
    assert row is not None, "nothing was written"
    assert row.user_id == C1_USER, (
        f"the row is attributed to {row.user_id!r}; the caller named 'U-owner' and the "
        f"link says {C1_USER!r}")
    assert row.role != "owner", (
        f"the caller chose the role recorded against the action: role={row.role!r}")
    assert row.tg_username != "impersonated_owner", (
        f"the caller chose the Telegram username on the row: {row.tg_username!r}")
    assert row.ip != "203.0.113.99", (
        f"the caller chose the IP recorded against the action: {row.ip!r}")
    assert row.branch == "Store A", (
        f"an in-scope branch must still be recorded — it is the one thing the bot "
        f"genuinely knows and the server cannot derive: {row.branch!r}")
    # Content the bot reported IS preserved — the fix is a split, not a blanket
    # refusal to record what the bot says happened.
    assert row.action == "approve" and row.entity == "purchase"
    assert row.detail == "approved by owner"


def test_2b_a_branch_outside_the_actors_scope_is_refused_not_recorded():
    """WHERE the action happened is caller-supplied — it has to be, because an
    all-branch owner acts at a different branch every day and the server cannot
    derive it. So it is CHECKED rather than trusted or discarded.

    Refusal is deliberate: silently recording NULL would let the forgery attempt
    succeed as an ordinary-looking row, and silently substituting the "right" branch
    would record an event that nobody claimed.
    """
    before = _count()
    r = client.post("/api/telegram/audit",
                    json={"tg_id": C1_TG, "branch": "Store C",     # U-cash holds Store A only
                          "action": "approve", "entity": "purchase", "ref": "H9-FORGE-2"},
                    headers=_bot())
    assert r.status_code == 403, (
        f"a branch outside the acting user's scope was accepted: {r.status_code}")
    assert _latest("H9-FORGE-2") is None, "the refused call still wrote a row"
    assert _count() == before


def test_3_the_row_lands_in_the_linked_accounts_tenant_not_company_1():
    r = client.post("/api/telegram/audit",
                    json={"tg_id": C2_TG, "action": "clock_in", "entity": "attendance",
                          "ref": "H9-TENANT-1", "detail": "company 2 activity"},
                    headers=_bot())
    assert r.status_code == 200, r.text
    row = _latest("H9-TENANT-1")
    assert row is not None
    assert row.company_id == C2, (
        f"a company-{C2} Telegram action was recorded as company {row.company_id}'s "
        f"audit history (the column server default is 1, and an unauthenticated "
        f"session gives _stamp_writes nothing to apply)")
    assert row.user_id == C2_USER


def test_4_an_unlinked_or_disabled_tg_id_is_refused_not_recorded_anonymously():
    """A row that cannot be attributed must not be written at all. Recording it
    with empty attribution would put an unattributable entry in the trail and
    leave the tenant to the column default."""
    before = _count()

    r = client.post("/api/telegram/audit",
                    json={"tg_id": "999999999", "action": "approve", "ref": "H9-UNLINKED"},
                    headers=_bot())
    assert r.status_code == 403, f"an unlinked tg_id got {r.status_code}"

    r = client.post("/api/telegram/audit",
                    json={"action": "approve", "ref": "H9-NOTGID"}, headers=_bot())
    assert r.status_code in (403, 422), f"a missing tg_id got {r.status_code}"

    with tenancy.system_session() as db:
        link = db.get(models.TelegramLink, C1_TG)
        link.status = "disabled"
        db.commit()
    try:
        r = client.post("/api/telegram/audit",
                        json={"tg_id": C1_TG, "action": "approve", "ref": "H9-DISABLED"},
                        headers=_bot())
        assert r.status_code == 403, f"a disabled link got {r.status_code}"
    finally:
        with tenancy.system_session() as db:
            db.get(models.TelegramLink, C1_TG).status = "active"
            db.commit()

    assert _count() == before, "a refused call still wrote an audit row"
    for ref in ("H9-UNLINKED", "H9-NOTGID", "H9-DISABLED"):
        assert _latest(ref) is None


# ===========================================================================
# 2. The limit, asserted rather than merely written down.
# ===========================================================================
def test_5_a_bot_token_holder_can_still_act_for_any_LINKED_account():
    """This is NOT a regression — it is the residual risk, pinned so it cannot be
    quietly forgotten or quietly claimed as closed.

    Whoever holds the single shared bot token can attribute an action to any tg_id
    that really is linked. Closing that needs per-user signing. If someone later
    implements it, this test starts failing and that failure is the signal to
    delete it and record the improvement — not to weaken the fix.
    """
    r = client.post("/api/telegram/audit",
                    json={"tg_id": C2_TG, "action": "clock_in", "entity": "attendance",
                          "ref": "H9-RESIDUAL"},
                    headers=_bot())
    assert r.status_code == 200, (
        "the residual risk appears to be closed — per-user authentication may now "
        "exist on this endpoint; verify and remove this test rather than relaxing it")
    row = _latest("H9-RESIDUAL")
    assert row is not None and row.user_id == C2_USER


def test_6_the_token_comparison_is_constant_time_everywhere():
    """The endpoint compared with `!=` while `/auth-token` used
    `hmac.compare_digest`. A shared secret checked fifteen different ways is
    checked as well as its weakest site, so there is now ONE implementation.

    Asserted structurally rather than by timing: a timing assertion in a test suite
    is flaky, and what actually went wrong here was divergence between call sites,
    not the measured timing of any one of them.
    """
    import ast
    import inspect
    from app import security as S
    from app.routers import telegram as T

    assert hasattr(S, "require_bot_token"), "the shared bot-token check is gone"

    # Nothing in the bot-facing routers may open-code a comparison against the
    # server-side token any more.
    for mod in (T,):
        src = inspect.getsource(mod)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and any(
                    isinstance(op, (ast.NotEq, ast.Eq)) for op in node.ops):
                seg = ast.get_source_segment(src, node) or ""
                assert "bot_token" not in seg or "settings.bot_token" not in seg, (
                    f"{mod.__name__} open-codes a bot-token comparison: {seg!r}. Use "
                    f"security.require_bot_token so every site is checked identically.")

    assert settings.bot_token, "fixture: the server-side token must be set"
