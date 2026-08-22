"""SEC-11 + SEC-12 regression — the Control Center's own credential must be at least
as well defended as a cashier's, and its realm must be a real boundary.

**SEC-11 (HIGH)** — `pfs/auth/login` had neither control the tenant login has had
since SS-H-007. No throttle, and `repo.audit(...)` called only on SUCCESS. The
auditor measured it side by side, same process, same probe:

```
30 x /pfs/auth/login : [401]        any 429? False
30 x /api/auth/login : [401, 429]   any 429? True
platform_audit rows after 30 failures: []
```

The most privileged credential in the system — a platform super admin, who reaches
every tenant — could be attacked without bound and **without leaving any trace**,
while a cashier's could not. Both halves matter: the throttle bounds the attack, the
failure record is what makes it visible afterwards. An unrecorded failed login is an
attack that leaves the system looking untouched.

**SEC-12 (HIGH)** — `pfs/config.py` falls `PFS_JWT_SECRET` back to `JWT_SECRET` so a
co-hosted deployment needs no extra environment variable. The module docstring asserts
tokens "never cross between the tenant app and the Control Center". They do not cross
*as issued* — the realm check genuinely works — but a token **forged** with the ERP
secret and `realm="pfs"` authenticates as a platform super admin.

The precondition is real and is stated rather than hidden: this needs the ERP signing
key, and whoever holds that already owns the ERP. What is lost is the CONTAINMENT the
design claims — a tenant-application compromise should not also be a platform
compromise. The fix is therefore at the startup gate, which already refuses to boot a
production store on a default `JWT_SECRET`: `production_secret_problems()` now also
reports a PFS secret that is unset, default, or **equal to the ERP's**.

Deriving the PFS key from the ERP key instead would be no fix: the derivation is in
the source, so holding `JWT_SECRET` still yields it. Tests 5-6 assert the gate reports
the problem; they do not assert that forging becomes impossible, because with a shared
key it is not — which is exactly what the gate exists to prevent a deployment from
choosing.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"sec1112_pfs_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("SEED_ON_START", "true")
os.environ.setdefault("SEED_DEMO_DATA", "true")
os.environ.setdefault("JWT_SECRET", "sec1112-secret-long-enough-for-tests")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")

import pytest                                        # noqa: E402
from fastapi.testclient import TestClient            # noqa: E402

from app.main import app                             # noqa: E402
from app.config import settings                      # noqa: E402
from app import models, tenancy                      # noqa: E402
from app.pfs import security as PS                   # noqa: E402
from app.pfs.config import pfs_config                # noqa: E402

with TestClient(app):
    pass
client = TestClient(app)

SA = "SA-sec1112"
SA_PW = "Platform-Pass-5521"


def setup_module(_m):
    from app.pfs import security as _ps
    with tenancy.system_session() as db:
        if not db.query(models.PlatformUser).filter(
                models.PlatformUser.username == SA).first():
            db.add(models.PlatformUser(id=SA, username=SA, name="Sec1112 Root",
                                       password_hash=_ps.hash_pw(SA_PW),
                                       active=True))
        db.commit()


def teardown_module(_m):
    with tenancy.system_session() as db:
        db.query(models.PlatformAudit).filter(
            models.PlatformAudit.ref == SA).delete(synchronize_session=False)
        db.query(models.PlatformUser).filter(
            models.PlatformUser.username == SA).delete(synchronize_session=False)
        db.query(models.RateHit).delete()
        db.commit()


@pytest.fixture(autouse=True)
def _clean_throttle():
    """This module deliberately fills the pfs_login buckets. Clear them either side
    so it neither inherits nor exports a throttled state."""
    with tenancy.system_session() as db:
        db.query(models.RateHit).delete()
        db.commit()
    yield
    with tenancy.system_session() as db:
        db.query(models.RateHit).delete()
        db.commit()


def _platform_audit(action=None):
    with tenancy.system_session() as db:
        q = db.query(models.PlatformAudit).filter(models.PlatformAudit.ref == SA)
        if action:
            q = q.filter(models.PlatformAudit.action == action)
        return q.all()


# ===========================================================================
# SEC-11 — throttle and failure record.
# ===========================================================================
def test_0_a_correct_control_center_credential_still_signs_in():
    """Liveness. A throttle that refuses everybody would satisfy test 1."""
    r = client.post("/pfs/auth/login", data={"username": SA, "password": SA_PW})
    assert r.status_code == 200, r.text
    assert r.json().get("access_token")


def test_1_the_platform_login_is_throttled():
    """The auditor's exact comparison: the tenant login 429s and this one did not."""
    codes = []
    for _ in range(30):
        r = client.post("/pfs/auth/login", data={"username": SA, "password": "wrong"})
        codes.append(r.status_code)
        if r.status_code == 429:
            break
    assert 429 in codes, (
        f"30 failed Control Center logins were never throttled ({sorted(set(codes))}). "
        f"The most privileged credential in the system is attackable without bound "
        f"while a cashier's is not.")
    assert codes[0] == 401, "the first attempt should be a plain refusal, not a 429"


def test_2_a_failed_platform_login_is_recorded():
    """Independent of the throttle, and asserted separately: bounding an attack and
    leaving evidence of it are different properties, and this endpoint had neither."""
    before = len(_platform_audit())
    r = client.post("/pfs/auth/login", data={"username": SA, "password": "wrong"})
    assert r.status_code == 401, r.text

    rows = _platform_audit("failed_login")
    assert rows, (
        "a failed Control Center login wrote no platform_audit row — an attack on the "
        "platform credential leaves the system looking untouched")
    assert len(_platform_audit()) > before


def test_3_the_failure_record_does_not_confirm_which_usernames_exist():
    """The record must not become an oracle. A failure is attributed to the SUPPLIED
    username, never to a resolved account id — otherwise the presence of a
    `super_admin_id` on the row tells an attacker the username was real."""
    r = client.post("/pfs/auth/login",
                    data={"username": "no-such-platform-user", "password": "wrong"})
    assert r.status_code == 401

    with tenancy.system_session() as db:
        rows = db.query(models.PlatformAudit).filter(
            models.PlatformAudit.action == "failed_login").all()
    assert rows, "no failure rows at all"
    assert all(x.super_admin_id is None for x in rows), (
        "a failed login was attributed to a resolved super-admin id; the audit log "
        "then confirms which usernames exist")


def test_4_a_successful_login_is_still_recorded():
    """The control the endpoint already had must survive the change."""
    r = client.post("/pfs/auth/login", data={"username": SA, "password": SA_PW})
    assert r.status_code == 200, r.text
    assert _platform_audit("login"), "successful platform logins stopped being audited"


# ===========================================================================
# SEC-12 — the realm must not share the ERP's signing key.
# ===========================================================================
def test_5_the_realm_rule_reports_a_shared_signing_secret():
    """The rule lives on the Control Center (`pfs_config.secret_problems`) and is
    COMPOSED by `main.py`, the one module allowed to know about both sides.

    It was first written into `settings.production_secret_problems()`, which meant
    `app/config.py` importing `app/pfs` — and `test_pfs_decoupling.py` failed, exactly
    as it should: the sub-app is decoupled so it can be extracted to its own service.
    The ERP secret is now passed IN as an argument instead.
    """
    default = settings._DEFAULT_JWT_SECRET
    prior = pfs_config.jwt_secret
    try:
        # (a) shared with the ERP — the SEC-12 configuration
        pfs_config.jwt_secret = settings.jwt_secret
        problems = pfs_config.secret_problems(settings.jwt_secret, default)
        assert any("PFS_JWT_SECRET" in p and "same value" in p for p in problems), (
            f"one signing key across both realms was reported as sound: {problems}")

        # (b) the development default
        pfs_config.jwt_secret = default
        assert pfs_config.secret_problems("something-else", default), (
            "the insecure development default was reported as sound")

        # (c) distinct — the rule must not fire on a correct configuration
        pfs_config.jwt_secret = "a-genuinely-different-platform-secret-value"
        assert not pfs_config.secret_problems(settings.jwt_secret, default), (
            "the rule fires on a correctly separated pair of secrets — a false FAIL "
            "costs the same credibility as a false pass")
    finally:
        pfs_config.jwt_secret = prior


def test_6_the_startup_gate_composes_the_realm_rule():
    """The rule existing is not the same as the gate consulting it. This asserts the
    composition root actually calls it — a rule nothing invokes is a comment.

    Asserted on `main.py`'s source because the startup handler deliberately skips
    itself under pytest (it must not refuse to boot the test suite), so the call
    cannot be observed by running it.
    """
    import inspect

    from app import main as M

    src = inspect.getsource(M)
    assert "secret_problems(" in src and "production_secret_problems()" in src, (
        "main.py no longer composes the Control Center's realm rule into the "
        "fail-closed startup gate")
    assert settings.is_sqlite, "fixture expects the dev store"
    assert not settings.production_secret_problems(), (
        "the dev store must still boot with no extra environment — breaking that "
        "gets the check deleted rather than obeyed")


def test_7_the_realm_check_itself_still_works():
    """Not a regression test for SEC-12 — a control for it.

    SEC-12 is NOT "the realm check is broken". Honestly-issued tokens genuinely do not
    cross. Asserting that here keeps the finding stated accurately: what fails is
    containment against someone holding the ERP secret, not the realm mechanism.
    """
    erp = client.post("/api/auth/login",
                      data={"username": "U-owner", "password": "demo1234"})
    assert erp.status_code == 200, erp.text
    erp_tok = erp.json()["access_token"]
    assert client.get("/pfs/auth/me",
                      headers={"Authorization": "Bearer " + erp_tok}).status_code == 401

    pfs = client.post("/pfs/auth/login", data={"username": SA, "password": SA_PW})
    assert pfs.status_code == 200, pfs.text
    assert client.get("/api/auth/me",
                      headers={"Authorization": "Bearer " + pfs.json()["access_token"]}
                      ).status_code == 401


def test_8_the_shared_key_really_does_forge_a_platform_token():
    """The finding, demonstrated rather than asserted away — and the reason the fix is
    a configuration gate and not a code change.

    While the two secrets are equal, a token signed with the ERP key and `realm="pfs"`
    IS a valid platform token. No code change makes that false; only separating the
    keys does. If this test ever starts failing because the keys were separated by
    default, that is an improvement — replace it, do not weaken it.
    """
    from datetime import datetime, timedelta, timezone

    from jose import jwt

    if pfs_config.jwt_secret != settings.jwt_secret:
        pytest.skip("the two realms already have distinct secrets in this environment")

    forged = jwt.encode({"sub": SA, "realm": "pfs",
                         "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
                        settings.jwt_secret, algorithm=settings.jwt_alg)
    r = client.get("/pfs/auth/me", headers={"Authorization": "Bearer " + forged})
    assert r.status_code == 200, (
        "the forgery no longer works — verify why and update this test rather than "
        "deleting the startup gate")
    assert r.json().get("username") == SA
