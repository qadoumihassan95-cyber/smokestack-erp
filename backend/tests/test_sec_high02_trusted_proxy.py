"""SEC HIGH-02 regression — the per-IP half of every throttle must not be keyed
on a value the caller writes.

At `3eb1ccd`, `ratelimit.client_ip()` returned the FIRST `X-Forwarded-For` entry
unconditionally, with no trusted-proxy allowlist. `X-Forwarded-For` is entirely
client-supplied, so rotating it once per attempt gave every request its own
per-IP bucket. ERP-Security-Auditor executed 120 `POST /api/auth/login` attempts,
one guess per distinct account, rotating `X-Forwarded-For: 203.0.113.N`, and
**none was throttled** — defeating the per-IP half of SS-H-007 for password
spraying against every account at once.

The fix is a hop count, not a blanket refusal to read the header, and the reason
is in test 4: deployed behind Render, ignoring the header entirely puts the whole
customer base in ONE bucket keyed on the router's address, so the per-IP limit
would throttle every tenant at once. `TRUSTED_PROXY_HOPS` says how many proxies
we actually operate; the entry that many from the RIGHT is the one our own proxy
wrote, and everything an attacker prepends sits to the left of it and is ignored.

Tests 1 and 5 are the ones that fail at `3eb1ccd`. Tests 2-4 and 6 pin the
behaviour the hop count introduces, and test 0 is the liveness control.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"sec_h02_xff_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("SEED_ON_START", "true")
os.environ.setdefault("SEED_DEMO_DATA", "true")
os.environ.setdefault("JWT_SECRET", "sec-h02-secret-long-enough-for-tests")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")

import pytest                                       # noqa: E402
from fastapi.testclient import TestClient           # noqa: E402

from app.main import app                            # noqa: E402
from app.config import settings                     # noqa: E402
from app import models, ratelimit as RL, tenancy    # noqa: E402

with TestClient(app):
    pass
client = TestClient(app)

# guard(kind="login", limit=10) throttles the IP scope at limit * 3.
IP_LIMIT = 30


class _FakeReq:
    """Minimal stand-in for a Starlette Request: a socket peer and headers."""

    class _Client:
        def __init__(self, host):
            self.host = host

    def __init__(self, peer="10.0.0.9", xff=None):
        self.client = self._Client(peer) if peer else None
        self.headers = {"x-forwarded-for": xff} if xff else {}


def _hops(n):
    """Set the deployment topology for one assertion, and put it back."""
    prior = getattr(settings, "trusted_proxy_hops", 0)
    settings.trusted_proxy_hops = n
    return prior


def _clear_hits():
    """RateHit rows live in the process-wide database every module shares. This
    module deliberately fills the `testclient` peer bucket, so it clears before
    and after rather than leaving other modules to be throttled by its attack."""
    with tenancy.system_session() as db:
        db.query(models.RateHit).delete()
        db.commit()


@pytest.fixture(autouse=True)
def _isolate_throttle_state():
    _clear_hits()
    prior = getattr(settings, "trusted_proxy_hops", 0)
    yield
    settings.trusted_proxy_hops = prior
    _clear_hits()


# ===========================================================================
# 0. LIVENESS — the limiter engages at all.
# ===========================================================================
def test_0_the_per_ip_limiter_engages_when_the_address_is_stable():
    """If this fails, every 'was not throttled' assertion below is meaningless:
    it would mean the limiter never fires, not that a header defeated it."""
    settings.trusted_proxy_hops = 0          # header ignored; peer is `testclient`
    saw_429 = False
    for i in range(IP_LIMIT + 10):
        r = client.post("/api/auth/login",
                        data={"username": f"nobody-live-{i}", "password": "wrong"})
        if r.status_code == 429:
            saw_429 = True
            break
        assert r.status_code == 401, f"unexpected {r.status_code}: {r.text[:200]}"
    assert saw_429, (
        f"{IP_LIMIT + 10} failed logins from one address were never throttled — the "
        f"per-IP limiter is not working, so this module cannot measure anything")


# ===========================================================================
# 1. THE FINDING — a rotated header must not buy a fresh bucket.
# ===========================================================================
def test_1_rotating_x_forwarded_for_cannot_evade_the_per_ip_throttle():
    """The auditor's 120-attempt password spray, in miniature.

    Each attempt targets a DIFFERENT account, so the per-identifier limit (which
    is not IP-derived and still holds) can never be what stops it. The only thing
    that can is the per-IP limit — and at 3eb1ccd the attacker simply relabelled
    themselves before each attempt.
    """
    settings.trusted_proxy_hops = 0          # the secure default
    saw_429 = False
    attempts = 0
    for i in range(IP_LIMIT + 15):
        attempts += 1
        r = client.post("/api/auth/login",
                        data={"username": f"victim-{i}", "password": "wrong"},
                        headers={"X-Forwarded-For": f"203.0.113.{i % 250}"})
        if r.status_code == 429:
            saw_429 = True
            break
        assert r.status_code == 401, f"unexpected {r.status_code}: {r.text[:200]}"

    assert saw_429, (
        f"{attempts} failed logins, each against a different account and each "
        f"declaring a different X-Forwarded-For, were never throttled. The per-IP "
        f"half of SS-H-007 is keyed on a value the attacker writes.")


def test_2_a_forged_header_cannot_evade_the_throttle_even_behind_a_trusted_proxy():
    """With TRUSTED_PROXY_HOPS=1 the RIGHTMOST entry is the address our own proxy
    observed. An attacker prepending entries only lengthens the part we skip."""
    settings.trusted_proxy_hops = 1
    saw_429 = False
    for i in range(IP_LIMIT + 15):
        r = client.post("/api/auth/login",
                        data={"username": f"spray-{i}", "password": "wrong"},
                        headers={"X-Forwarded-For": f"203.0.113.{i % 250}, 198.51.100.7"})
        if r.status_code == 429:
            saw_429 = True
            break
        assert r.status_code == 401, f"unexpected {r.status_code}: {r.text[:200]}"
    assert saw_429, (
        "an attacker prepending a rotating entry in front of the proxy-written one "
        "was never throttled — the wrong end of the chain is being read")


# ===========================================================================
# 2. The resolver itself.
# ===========================================================================
def test_3_default_topology_ignores_the_header_entirely():
    settings.trusted_proxy_hops = 0
    assert RL.client_ip(_FakeReq(peer="10.0.0.9", xff="203.0.113.5")) == "10.0.0.9"
    assert RL.client_ip(_FakeReq(peer="10.0.0.9", xff="1.1.1.1, 2.2.2.2")) == "10.0.0.9"


def test_4_one_trusted_proxy_reads_the_entry_that_proxy_wrote():
    """This is why the fix is a hop count and not `never trust XFF`.

    Behind Render every request's socket peer is the router. Ignoring the header
    there would put every tenant in one bucket and throttle the whole customer
    base together — so the deployed config must be able to read the real client
    address, and must read the ONE entry an attacker cannot control.
    """
    settings.trusted_proxy_hops = 1
    # Only the proxy wrote: that entry is the client.
    assert RL.client_ip(_FakeReq(peer="10.0.0.9", xff="203.0.113.5")) == "203.0.113.5"
    # Attacker prepended two forged entries; the rightmost is still the proxy's.
    assert RL.client_ip(
        _FakeReq(peer="10.0.0.9", xff="1.1.1.1, 2.2.2.2, 203.0.113.5")) == "203.0.113.5"
    # Two proxies configured, two present.
    settings.trusted_proxy_hops = 2
    assert RL.client_ip(
        _FakeReq(peer="10.0.0.9", xff="203.0.113.5, 10.1.1.1")) == "203.0.113.5"


def test_5_a_chain_shorter_than_the_configured_hops_falls_back_to_the_peer():
    """A caller who sends no header, or fewer entries than we expect, must not be
    able to make us attribute the request to whatever they did send. Nothing in a
    chain that is not the shape we configured is attributable, so we use the peer."""
    settings.trusted_proxy_hops = 2
    assert RL.client_ip(_FakeReq(peer="10.0.0.9", xff="203.0.113.5")) == "10.0.0.9"
    assert RL.client_ip(_FakeReq(peer="10.0.0.9", xff="")) == "10.0.0.9"
    assert RL.client_ip(_FakeReq(peer="10.0.0.9", xff=None)) == "10.0.0.9"


def test_6_never_returns_a_caller_supplied_value_when_no_proxy_is_configured():
    """The property, stated once without enumerating chain shapes: at hops=0 the
    result is the peer for EVERY header the caller can construct.

    Written as a property rather than a list of examples on purpose — an
    enumerated list of shapes fails at the next member, and a header is exactly
    the kind of input whose shapes are unbounded.
    """
    settings.trusted_proxy_hops = 0
    hostile = ["203.0.113.5", " 203.0.113.5 ", "1.1.1.1, 2.2.2.2, 3.3.3.3",
               ",,,", "  ", "::1", "not-an-ip", "10.0.0.9, 203.0.113.5"]
    for xff in hostile:
        got = RL.client_ip(_FakeReq(peer="10.0.0.9", xff=xff))
        assert got == "10.0.0.9", f"X-Forwarded-For={xff!r} produced {got!r}"
