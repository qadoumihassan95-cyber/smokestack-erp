"""SEC CRITICAL-01 regression — the idempotency cache is an authentication and
authorization boundary, and must behave like one.

`IdempotencyMiddleware` is the OUTERMOST middleware. Anything it answers is
answered before routing, before `get_current_user`, and before every
`require(...)` dependency. At `3eb1ccd` its lookup was `(scope, key)` where
`scope` collapsed to the literal `"anon"` for any request with no `Authorization`
header — one namespace shared by every unauthenticated caller on the internet —
and `method`/`path` were persisted but never compared.

Three consequences were executed by ERP-Security-Auditor against `3eb1ccd`
(`RESEARCH/security-probes/smokestack-3eb1ccd/test_sec_probe_a_idempotency.py`,
preserved verbatim; that module asserts the VULNERABLE behaviour and passes
there). This module asserts the SECURE behaviour, so every test here is the
inverse of a probe-A result and FAILS at `3eb1ccd`:

    A1  an unauthenticated caller with wrong credentials received the victim's
        live bearer token, cross-tenant, HTTP 200
    A2  one key made an unrelated later mutation return the earlier operation's
        success body while never executing — a financial write reported as done
    A3  an unauthenticated POST to a route requiring `create` was answered 200
        from the cache instead of 401

Tests 6-9 cover properties the probe did not reach but the fix introduces:
revoked-token scope separation, failure responses never being cached, TTL
expiry, and cross-tenant namespace separation.

TEST 0 IS LOAD-BEARING. Every other test here would also pass if the middleware
were simply deleted, so this module opens by proving idempotency still WORKS —
a real duplicate retry is still suppressed and the effect still happens exactly
once. A security regression suite that a `return await call_next(request)` would
satisfy is not measuring the fix.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

_DB = os.path.join(tempfile.gettempdir(), f"sec_c01_idem_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("SEED_ON_START", "true")
os.environ.setdefault("SEED_DEMO_DATA", "true")
os.environ.setdefault("JWT_SECRET", "sec-c01-secret-long-enough-for-tests")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")

from fastapi.testclient import TestClient          # noqa: E402
from jose import jwt                                # noqa: E402

from app.main import app                            # noqa: E402
from app.config import settings                     # noqa: E402
from app import models, security, tenancy           # noqa: E402

C2 = 2
C2_OWNER = "V-owner"
C2_PW = "Victim-Pass-8821"
PW = "demo1234"

# Read defensively so this module produces a real ASSERTION FAILURE when run
# against 3eb1ccd (where no TTL exists) rather than an AttributeError. A red that
# says "an expired entry was replayed" is evidence; a red that says the attribute
# is missing only says the code is different.
_TTL = int(getattr(settings, "idempotency_ttl_hours", 24) or 24)

with TestClient(app):
    pass
client = TestClient(app)


def setup_module(_m):
    """A second company with its own owner, so a replay that crosses namespaces
    can be shown to cross a TENANT boundary and not merely a session boundary."""
    _clear_login_throttle()
    with tenancy.system_session() as db:
        if not db.query(models.Company).filter(models.Company.id == C2).first():
            db.add(models.Company(id=C2, name="Victim Co", slug="victim-co-idem",
                                  application_key="smoke_shop",
                                  owner_user_id=C2_OWNER, status="active"))
        db.commit()
    with tenancy.tenant_session(C2) as db:
        if not db.get(models.User, C2_OWNER):
            db.add(models.User(id=C2_OWNER, name="Victim Owner", role="owner",
                               password_hash=security.hash_pw(C2_PW),
                               status="active", can_login=True))
        if not db.query(models.Branch).filter(models.Branch.name == "Victim Depot").first():
            db.add(models.Branch(name="Victim Depot", timezone="UTC"))
        db.commit()


def teardown_module(_m):
    """Test modules share ONE process-wide engine and database file, so anything
    this module creates is visible to every module that runs after it. Drop the
    cache rows, this module's purchases, and its branch — a stray branch shows up
    in other modules' branch-label and branch-count assertions."""
    with tenancy.system_session() as db:
        db.query(models.IdempotencyKey).delete()
        db.query(models.Purchase).filter(models.Purchase.branch == "Victim Depot").delete(
            synchronize_session=False)
        db.query(models.Branch).filter(models.Branch.name == "Victim Depot").delete(
            synchronize_session=False)
        db.commit()


def _clear_login_throttle():
    """Drop accumulated rate-limit hits before a deliberate bad-credentials call.

    Necessary because of THIS candidate's own HIGH-02 fix. `client_ip()` no longer
    trusts `X-Forwarded-For` by default, and under `TestClient` every request's socket
    peer is the literal string `testclient` — so every module's failed logins now share
    ONE per-IP bucket instead of the distinct forged addresses they used to get. By the
    time this module runs in a full-suite pass, that bucket can already be at its limit,
    and an attack expecting 401 gets 429 instead.

    This is a test-harness artefact, not a production one: real clients have distinct
    peer addresses, and the deployed configuration sets TRUSTED_PROXY_HOPS=1 so the
    proxy-observed client address is used. Clearing here keeps the assertion about
    replay rather than about throttling.
    """
    with tenancy.system_session() as db:
        db.query(models.RateHit).delete()
        db.commit()


def _login(uid, pw=PW, key=None):
    h = {"Idempotency-Key": key} if key else {}
    return client.post("/api/auth/login", data={"username": uid, "password": pw}, headers=h)


def _tok(uid, pw=PW):
    r = _login(uid, pw)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(tok, key=None):
    h = {"Authorization": "Bearer " + tok}
    if key:
        h["Idempotency-Key"] = key
    return h


def _purchase_count():
    with tenancy.system_session() as db:
        return db.query(models.Purchase).count()


def _keys_for(key):
    with tenancy.system_session() as db:
        return db.query(models.IdempotencyKey).filter(models.IdempotencyKey.key == key).all()


# ===========================================================================
# 0. LIVENESS CONTROL — the feature still does its job.
# ===========================================================================
def test_0_genuine_retry_is_still_replayed_and_executes_exactly_once():
    """Without this, a middleware that never caches anything would pass tests 1-9.

    A real client retry — same principal, same method, same path, same key —
    must be answered from the cache, and the side effect must have happened once.
    """
    tok = _tok("U-owner")
    key = "liveness-retry-key-0"
    before = _purchase_count()

    first = client.post("/api/purchases",
                        json={"vendor": "Acme", "branch": "Store A", "amount": 100},
                        headers=_h(tok, key))
    assert first.status_code == 201, first.text
    assert first.headers.get("Idempotency-Replayed") is None

    second = client.post("/api/purchases",
                         json={"vendor": "Acme", "branch": "Store A", "amount": 100},
                         headers=_h(tok, key))
    assert second.headers.get("Idempotency-Replayed") == "true", (
        "the retry was NOT replayed — idempotency is not working at all, so every "
        "other assertion in this module is vacuous")
    assert second.status_code == 201
    assert second.json() == first.json()
    assert _purchase_count() == before + 1, "the retry executed a second time"


# ===========================================================================
# 1. A1 — anon-namespace login replay (cross-tenant authentication bypass).
# ===========================================================================
def test_1_unauthenticated_caller_cannot_replay_a_victims_login_response():
    """At 3eb1ccd this returned HTTP 200 with company 2's owner bearer token to a
    caller who supplied `does-not-exist` / `wrong`."""
    KEY = "shared-anon-key-A1"
    _clear_login_throttle()

    victim = _login(C2_OWNER, C2_PW, key=KEY)
    assert victim.status_code == 200, victim.text
    victim_token = victim.json()["access_token"]
    vclaims = jwt.decode(victim_token, settings.jwt_secret, algorithms=[settings.jwt_alg])
    assert vclaims["sub"] == C2_OWNER and vclaims["company_id"] == C2, (
        "fixture is wrong: the victim login must be a real company-2 owner session")

    attacker = client.post("/api/auth/login",
                           data={"username": "does-not-exist", "password": "wrong"},
                           headers={"Idempotency-Key": KEY})

    assert attacker.headers.get("Idempotency-Replayed") is None, (
        "an unauthenticated caller was served a cached response")
    assert attacker.status_code == 401, (
        f"invalid credentials returned {attacker.status_code}; the real login handler "
        f"never ran")
    assert "access_token" not in attacker.text, "a bearer token reached the attacker"
    assert victim_token not in attacker.text, "the VICTIM's bearer token reached the attacker"


def test_2_an_unauthenticated_response_is_never_persisted_at_all():
    """The half of the fix that is not visible in a status code.

    Refusing to REPLAY to an anonymous caller while still STORING anonymous
    responses would leave the same primitive in place for anyone who can later
    present any token. Nothing without a live principal enters the table in
    either direction.
    """
    KEY = "anon-must-not-be-stored-2"
    r = _login(C2_OWNER, C2_PW, key=KEY)
    assert r.status_code == 200, r.text          # the login itself succeeded
    assert _keys_for(KEY) == [], (
        "a login response was written to idempotency_keys; logins are unauthenticated "
        "at the moment the middleware sees them, so this row is exactly the anon-scope "
        "record that made A1 possible")


# ===========================================================================
# 2. A3 — replay must not answer before authentication on a protected route.
# ===========================================================================
def test_3_unauthenticated_post_to_a_protected_route_is_401_not_a_cached_body():
    KEY = "anon-protected-key-A3"
    _clear_login_throttle()

    seeded = _login("U-owner", key=KEY)
    assert seeded.status_code == 200

    r = client.post("/api/purchases",
                    json={"vendor": "X", "branch": "Store A", "amount": 1},
                    headers={"Idempotency-Key": KEY})

    assert r.headers.get("Idempotency-Replayed") is None
    assert r.status_code == 401, (
        f"an unauthenticated POST to a route requiring 'create' returned "
        f"{r.status_code}; the auth dependency did not run")
    assert "access_token" not in r.text


# ===========================================================================
# 3. A2 — a key belongs to ONE operation.
# ===========================================================================
def test_4_reusing_a_key_on_a_different_operation_is_refused_not_answered():
    """At 3eb1ccd the receive returned 201 with the PURCHASE's body and no stock
    moved: the caller was told 'created' for work that never happened.

    409 is the correct answer, and 'the write did not happen' is only acceptable
    BECAUSE the caller was told so — which is the whole difference from the defect.
    """
    tok = _tok("U-owner")
    KEY = "cross-path-key-A2"

    def _stock(sku="MRB-GLD", branch="Store A"):
        with tenancy.tenant_session(1) as db:
            st = (db.query(models.Stock)
                  .filter(models.Stock.sku == sku, models.Stock.branch == branch).first())
            return int(st.qty) if st else None

    first = client.post("/api/purchases",
                        json={"vendor": "Acme", "branch": "Store A", "amount": 100},
                        headers=_h(tok, KEY))
    assert first.status_code == 201, first.text

    before = _stock()
    assert before is not None, "fixture stock row missing"

    second = client.post("/api/inventory/receive",
                         json={"sku": "MRB-GLD", "branch": "Store A", "qty": 25},
                         headers=_h(tok, KEY))

    assert second.status_code == 409, (
        f"expected 409 for a key reused across operations, got {second.status_code}")
    assert second.json() != first.json(), "the unrelated purchase body was replayed"
    assert _stock() == before, "the receive ran despite the refusal"

    # And the refusal is not a dead end: a fresh key completes the real work.
    retry = client.post("/api/inventory/receive",
                        json={"sku": "MRB-GLD", "branch": "Store A", "qty": 25},
                        headers=_h(tok, "cross-path-key-A2-retry"))
    assert retry.status_code in (200, 201), retry.text
    assert _stock() == before + 25, "the caller could not complete the operation"


# ===========================================================================
# 4. Namespace separation — tenants, users, sessions, realms.
# ===========================================================================
def test_5_two_tenants_using_the_same_key_do_not_see_each_others_responses():
    KEY = "same-key-different-tenants-5"
    t1 = _tok("U-owner")
    t2 = _tok(C2_OWNER, C2_PW)

    a = client.post("/api/purchases",
                    json={"vendor": "Acme", "branch": "Store A", "amount": 100},
                    headers=_h(t1, KEY))
    assert a.status_code == 201, a.text

    b = client.post("/api/purchases",
                    json={"vendor": "Beta", "branch": "Victim Depot", "amount": 50},
                    headers=_h(t2, KEY))
    assert b.headers.get("Idempotency-Replayed") is None, (
        "company 2 was served company 1's cached response")
    assert b.status_code == 201, b.text
    assert b.json() != a.json()


def test_6_a_revoked_token_cannot_replay_the_session_it_belonged_to():
    """`token_version` is part of the scope, so logout / password reset / role
    change moves the caller to a different namespace.

    Without this a stolen pre-revocation token would still read the responses
    cached before its revocation — for the whole TTL, through a middleware that
    runs before the route's own revocation check.
    """
    tok = _tok("U-admin")
    KEY = "revoked-token-key-6"

    first = client.post("/api/purchases",
                        json={"vendor": "Acme", "branch": "Store A", "amount": 100},
                        headers=_h(tok, KEY))
    assert first.status_code == 201, first.text

    out = client.post("/api/auth/logout", headers=_h(tok))
    assert out.status_code in (200, 204), out.text

    replayed = client.post("/api/purchases",
                           json={"vendor": "Acme", "branch": "Store A", "amount": 100},
                           headers=_h(tok, KEY))
    assert replayed.headers.get("Idempotency-Replayed") is None, (
        "a revoked token replayed a response cached before its revocation")
    assert replayed.status_code == 401, (
        f"a revoked token got {replayed.status_code} instead of 401")


# ===========================================================================
# 5. What may be cached, and for how long.
# ===========================================================================
def test_7_a_failure_response_is_not_cached_and_does_not_become_sticky():
    """A 4xx has no effect to be idempotent about. Caching one made a transient
    failure permanent for the whole TTL, on a key the caller could not clear."""
    tok = _tok("U-owner")
    KEY = "failure-not-sticky-7"

    bad = client.post("/api/purchases", json={"vendor": "Acme", "branch": "Store A"},
                      headers=_h(tok, KEY))
    assert bad.status_code >= 400, f"fixture expected a rejection, got {bad.status_code}"
    assert _keys_for(KEY) == [], "a failed response was cached"

    good = client.post("/api/purchases",
                       json={"vendor": "Acme", "branch": "Store A", "amount": 100},
                       headers=_h(tok, KEY))
    assert good.headers.get("Idempotency-Replayed") is None
    assert good.status_code == 201, (
        f"the corrected retry was answered {good.status_code} — the earlier failure "
        f"was replayed")


def test_8_an_entry_past_its_ttl_is_not_replayed():
    tok = _tok("U-owner")
    KEY = "ttl-expiry-key-8"
    before = _purchase_count()

    first = client.post("/api/purchases",
                        json={"vendor": "Acme", "branch": "Store A", "amount": 100},
                        headers=_h(tok, KEY))
    assert first.status_code == 201, first.text
    rows = _keys_for(KEY)
    assert len(rows) == 1, f"expected exactly one cached row, got {len(rows)}"

    aged = datetime.now(timezone.utc) - timedelta(hours=_TTL + 1)
    with tenancy.system_session() as db:
        row = (db.query(models.IdempotencyKey)
               .filter(models.IdempotencyKey.key == KEY).first())
        row.created_at = aged
        db.commit()

    second = client.post("/api/purchases",
                         json={"vendor": "Acme", "branch": "Store A", "amount": 100},
                         headers=_h(tok, KEY))
    assert second.headers.get("Idempotency-Replayed") is None, (
        "an entry older than IDEMPOTENCY_TTL_HOURS was replayed")
    assert second.status_code == 201
    assert _purchase_count() == before + 2, "the expired key still suppressed the write"


def test_9_expired_entries_are_pruned_so_the_table_stays_bounded():
    tok = _tok("U-owner")
    stale = datetime.now(timezone.utc) - timedelta(hours=_TTL + 5)
    with tenancy.system_session() as db:
        db.add(models.IdempotencyKey(scope="stale-scope-9", key="stale-key-9",
                                     method="POST", path="/api/purchases",
                                     status_code=201, response_body="{}",
                                     content_type="application/json",
                                     created_at=stale))
        db.commit()
    assert len(_keys_for("stale-key-9")) == 1

    r = client.post("/api/purchases",
                    json={"vendor": "Acme", "branch": "Store A", "amount": 100},
                    headers=_h(tok, "prune-trigger-key-9"))
    assert r.status_code == 201, r.text

    assert _keys_for("stale-key-9") == [], (
        "an entry past its TTL survived a later request — idempotency_keys grows "
        "without bound")
