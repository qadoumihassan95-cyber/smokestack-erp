"""SEC HIGH-07 regression — `POST /api/payroll/finalize` must not return per-employee
pay to a role that is refused the payroll read.

At `3eb1ccd`, `finalize` was guarded by `require("run_payroll")` and then reached the
figures by calling `payroll(start, end, branch, db, user)` **as a plain Python
function**. `payroll`'s own guard was `user = Depends(S.require("view_payroll"))` — a
FastAPI default argument, resolved by the router on HTTP dispatch and silently skipped
by a direct call. Passing `user` positionally skipped it entirely, and the endpoint
returned `{"ok": True, **s}` with the full unredacted `rows` list.

`branch_manager` and `manager` hold `run_payroll` and lack `view_payroll`. The auditor
executed it: `HTTP 200 rows=2 gross=5800  [Sam Rivera 3200/3200, Ana Gomez 2600/2600]`
— the same role refused by the read endpoint, served the identical figures by the
write endpoint.

WHY THE ASSERTIONS ARE VALUE-KEYED, NOT KEY-KEYED. Asserting `"rows" not in body`
would be satisfied by renaming the key. These tests plant nothing and instead walk
every scalar in the response looking for the employees' actual salaries and names as
they exist in the database. A defect that renames, restructures, nests or aggregates
its way around a key name still has to stop producing the number.

`S.redact_financials` is deliberately NOT the mechanism under test here and could not
be: it strips keys by NAME, the payroll figures are keyed `gross`/`net`, and the map
carries `gross_pay`/`net_pay` — so passing this payload through it removes nothing at
all while reading like a fix. Test 4 pins that trap directly.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"sec_h07_payroll_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("SEED_ON_START", "true")
os.environ.setdefault("SEED_DEMO_DATA", "true")
os.environ.setdefault("JWT_SECRET", "sec-h07-secret-long-enough-for-tests")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")

from fastapi.testclient import TestClient           # noqa: E402

from app.main import app                            # noqa: E402
from app import models, permissions as P, tenancy   # noqa: E402

with TestClient(app):
    pass
client = TestClient(app)

PW = "demo1234"
# branch_manager holds run_payroll (so finalize is legitimately reachable) and lacks
# view_payroll (so the read is legitimately refused). That gap IS the finding.
ATTACKER = "U-bm"
CONTROL = "U-acct"          # holds run_payroll AND view_payroll AND finalize_payroll


def _h(uid, pw=PW):
    r = client.post("/api/auth/login", data={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _scalars(o, out=None):
    """Every leaf value in a JSON structure, whatever the key names are."""
    out = [] if out is None else out
    if isinstance(o, dict):
        for v in o.values():
            _scalars(v, out)
    elif isinstance(o, list):
        for v in o:
            _scalars(v, out)
    else:
        out.append(o)
    return out


def _expected_disclosure(start, end, branch="Store A"):
    """The figures this endpoint actually produces for this period, obtained from
    the ENTITLED role's read.

    The first draft of this module compared against `employees.salary` straight from
    the database and it was a test that could not fail: `payroll` prorates
    (`salary * days / 30`), so for a 28-day period the raw 3200 never appears in the
    response at all and the assertion passed whether or not anything leaked. Deriving
    the expected set from the accountant's own GET — the exact computation, the exact
    period — means the comparison is against what would really be disclosed.

    Returns (names, figures) with small values dropped, so a coincidental `1` or a
    count cannot be mistaken for a salary.
    """
    r = client.get(f"/api/payroll?start={start}&end={end}&branch={branch}",
                   headers=_h(CONTROL))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("rows"), f"no figures exist for {start}..{end} — nothing to leak"
    names = {row["name"] for row in body["rows"]}
    figures = {float(v) for v in _scalars(body)
               if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) >= 100}
    assert names and figures, "the control read produced nothing to compare against"
    return names, figures


# ===========================================================================
# 0. CONTROLS — the permission gap this finding depends on is real.
# ===========================================================================
def test_0_the_role_matrix_is_what_the_finding_assumes():
    """If branch_manager ever gains view_payroll, tests 2-3 would pass for the
    wrong reason. State the precondition so a matrix change fails HERE, loudly,
    rather than silently making the regression vacuous."""
    assert P.can("branch_manager", "run_payroll"), "attacker cannot reach finalize"
    assert not P.can("branch_manager", "view_payroll"), (
        "branch_manager now holds view_payroll — this module no longer tests a "
        "disclosure to an unentitled role")
    assert P.can("accountant", "view_payroll") and P.can("accountant", "run_payroll")


def test_1_the_read_endpoint_refuses_the_attacker_and_serves_the_control():
    """The two halves of the contradiction, stated separately."""
    refused = client.get("/api/payroll?start=2026-01-01&end=2026-01-31",
                         headers=_h(ATTACKER))
    assert refused.status_code == 403, (
        f"GET /api/payroll returned {refused.status_code} to branch_manager; the "
        f"finding is that the WRITE endpoint contradicts this refusal")

    allowed = client.get("/api/payroll?start=2026-01-01&end=2026-01-31",
                         headers=_h(CONTROL))
    assert allowed.status_code == 200, allowed.text
    assert allowed.json().get("rows"), "control read returned no figures"


# ===========================================================================
# 1. THE FINDING.
# ===========================================================================
def test_2_finalize_does_not_disclose_pay_to_a_role_refused_the_read():
    START, END = "2026-02-01", "2026-02-28"
    names, figures = _expected_disclosure(START, END)

    r = client.post(f"/api/payroll/finalize?start={START}&end={END}&branch=Store A",
                    headers=_h(ATTACKER))
    assert r.status_code == 200, f"the action itself must still work: {r.text}"
    body = r.json()
    assert body.get("ok") is True, "the caller must still get a receipt for the run"

    leaves = _scalars(body)
    got_names = {v for v in leaves if isinstance(v, str)}
    got_figures = {float(v) for v in leaves
                   if isinstance(v, (int, float)) and not isinstance(v, bool)}

    leaked_names = names & got_names
    leaked_figures = figures & got_figures
    assert not leaked_names, (
        f"finalize disclosed employee names {sorted(leaked_names)} to a role refused "
        f"the payroll read")
    assert not leaked_figures, (
        f"finalize disclosed pay figures {sorted(leaked_figures)} to a role refused "
        f"the payroll read (these are the exact values the entitled role's GET "
        f"returns for the same period, including the run total — an aggregate of "
        f"protected figures is protected)")


def test_3_the_control_role_still_receives_the_figures():
    """The fix must not be 'stop returning payroll to everyone'. A role holding
    view_payroll gets exactly what it always did."""
    START, END = "2026-03-01", "2026-03-31"
    names, figures = _expected_disclosure(START, END)

    r = client.post(f"/api/payroll/finalize?start={START}&end={END}&branch=Store A",
                    headers=_h(CONTROL))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("rows"), (
        "a role holding view_payroll received no figures — the fix redacted the "
        "entitled caller too")

    leaves = _scalars(body)
    got_names = {v for v in leaves if isinstance(v, str)}
    got_figures = {float(v) for v in leaves
                   if isinstance(v, (int, float)) and not isinstance(v, bool)}
    assert names <= got_names, (
        f"the entitled caller lost employee names {sorted(names - got_names)}")
    assert figures <= got_figures, (
        f"the entitled caller lost pay figures {sorted(figures - got_figures)}")


# ===========================================================================
# 2. The trap that makes the obvious fix a non-fix.
# ===========================================================================
def test_4_the_name_keyed_redaction_map_does_not_cover_payroll_keys():
    """Pins WHY the fix gates the whole payload rather than calling
    redact_financials on it.

    If someone later "simplifies" hr.finalize to `return S.redact_financials(user,
    {...s})`, tests 2 would catch it — but only as a mysterious failure. This says
    the reason out loud and fails at the source: the map is keyed on names payroll
    does not use, so it is a no-op on this payload.

    `net` is deliberately NOT added to the map instead: reports_tg.py uses the same
    key for "net operating result", a profit figure, so one name would carry two
    different permissions. That ambiguity is the standing weakness of a name-keyed
    denylist, and is why the endpoint gates the payload as a whole.
    """
    from app import security as S

    assert "gross" not in S.FINANCIAL_FIELD_PERMS and "net" not in S.FINANCIAL_FIELD_PERMS, (
        "payroll's own key names are now in the map; if that was deliberate, check "
        "that `net` (also 'net operating result' in reports_tg.py) did not just get "
        "bound to the wrong permission")

    class _BM:
        role = "branch_manager"

    payload = {"rows": [{"name": "Sam Rivera", "gross": 3200, "net": 3200}], "gross": 3200}
    assert S.redact_financials(_BM(), payload) == payload, (
        "redact_financials now strips payroll keys — good, but hr.finalize must not "
        "start relying on it without re-checking every other consumer of `net`")
