"""BF-PR-01 / BF-PR-02 regressions — payroll must not post a figure it cannot compute,
and must not post one branch's money under another branch's name.

Both defects were independently reproduced by ERP-QA against frozen
`3eb1ccdea8457e2654d0dde1f81680c9339ea2b6`, and BF-PR-01 was reproduced on the real
API by ERP-Accounting-Auditor ($25/hr x 960 approved minutes, uncontested base pay
$400.00, durable `payroll_runs.gross = 0.00`, finalize returned success).

BF-PR-01 — `_payroll_figures` computes EVERY employee as `salary * days / 30` and never
reads `pay_type` or `hourly_rate`. An hourly employee therefore contributes 0, and that
0 was presented as a valid calculation and finalized into the ledger.

BF-PR-02 — `finalize` computed gross over the WHOLE resolved scope and then posted it
under `scope[0]`, while `branch: str = "all"` was the DEFAULT. A two-branch owner got
one PayrollRun labelled A carrying A+B; because the natural key is
`UNIQUE(company_id, branch, period_start, period_end)`, a later explicit `branch=B`
finalize for the same period was ACCEPTED and B was counted twice.

WHY THE PAY-TYPE GUARD IS AN ALLOW-LIST AND WHY THAT IS TESTED SEPARATELY.
`pay_type` is completely unvalidated end to end — `models.py` has no enum and no CHECK,
`schemas.py` has no validator, and `hr.py` writes the caller's string straight through.
A deny-list on `"hourly"` is therefore bypassed by `"Hourly"`, `"HOURLY"`, `" hourly "`,
`"commission"` or any typo, each of which produces the same uncomputable pay while
LOOKING like it was handled. Test 3 plants those exact values. Without it, the
allow-list's advantage over a deny-list is untested prose.

WHAT THESE TESTS DELIBERATELY DO NOT CLAIM.
* They do not show that any real company has hourly staff or has already posted a wrong
  figure. Detection of existing wrong postings is genuinely unsolved: a MIXED
  salaried/hourly branch posts a NONZERO aggregate that silently omits the hourly part,
  so scanning for `gross = 0` cannot clear occurrence. Honest detection needs
  re-deriving each finalized period against its roster, which needs the D4 rules that
  do not exist yet.
* They do not test the browser. Test 9 reads `index.html` as text and can only prove
  that a specific defective expression is absent and a specific masked state is
  present. What the user actually SEES is ERP-UX-Reviewer's gate, not this file's.
* Refusal deliberately writes NO audit row, because the accepted acceptance criterion
  is that ledger, payroll-run, audit and idempotency state are unchanged on refusal.
  That means a refused finalize leaves no trace; that tradeoff is recorded in the
  candidate log, not silently taken.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"bfpr_payroll_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("SEED_ON_START", "true")
os.environ.setdefault("SEED_DEMO_DATA", "false")
os.environ.setdefault("JWT_SECRET", "bfpr-payroll-secret-long-enough-for-tests")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")

import pytest                                        # noqa: E402
from fastapi.testclient import TestClient            # noqa: E402

from app.main import app                             # noqa: E402
from app import models, permissions as P             # noqa: E402
from app.database import SessionLocal                # noqa: E402

with TestClient(app):
    pass
client = TestClient(app)

PW = "demo1234"
OWNER = "U-owner"           # global role: resolve_branches returns EVERY branch
BM = "U-bm"                 # branch_manager: run_payroll WITHOUT view_payroll

START, END = "2031-03-01", "2031-03-30"


_TOKENS = {}


def _h(uid, pw=PW):
    """Tokens are cached, and that is load-bearing rather than an optimization.

    Logging in WRITES AN AUDIT ROW. Calling this inside a before/after comparison of
    `audit_log` made every "a refused finalize wrote nothing" assertion fail by
    exactly one row — a false FAIL produced entirely by the measuring instrument.
    The counts are the evidence, so the harness must not perturb what it counts.
    """
    if uid not in _TOKENS:
        r = client.post("/api/auth/login", data={"username": uid, "password": pw})
        assert r.status_code == 200, r.text
        _TOKENS[uid] = {"Authorization": "Bearer " + r.json()["access_token"]}
    return _TOKENS[uid]


def _mk(eid, name, branch, pay_type, salary=0, hourly_rate=0):
    """Create an employee through the REAL API, not a direct ORM insert.

    The product's path is `POST /api/employees`; a direct insert would let this
    fixture set values the API might reject and would prove nothing about what a
    caller can actually put in the database.
    """
    r = client.post("/api/employees", headers=_h(OWNER), json={
        "id": eid, "name": name, "branch": branch, "title": "Staff",
        "pay_type": pay_type, "salary": salary, "hourly_rate": hourly_rate})
    assert r.status_code == 201, r.text
    return r.json()


def _counts():
    """Durable row counts for every table a finalize touches."""
    db = SessionLocal()
    try:
        return {
            "ledger": db.query(models.Ledger).count(),
            "payroll_runs": db.query(models.PayrollRun).count(),
            "audit_log": db.query(models.AuditLog).count(),
        }
    finally:
        db.close()


def _runs():
    db = SessionLocal()
    try:
        return [(r.branch, float(r.gross), str(r.period_start), str(r.period_end))
                for r in db.query(models.PayrollRun).all()]
    finally:
        db.close()


def _scalars(o, out=None):
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


@pytest.fixture(scope="module", autouse=True)
def roster():
    """Store A: two salaried. Store B: one salaried. Store C: one hourly.

    Store A and Store B are both reachable by U-bm, which is what makes the
    multi-branch finalize in test 6 a real authorization-shaped scenario rather than
    an owner-only one.
    """
    _mk("E-A1", "Alpha One", "Store A", "salary", salary=3000)
    _mk("E-A2", "Alpha Two", "Store A", "salary", salary=1500)
    _mk("E-B1", "Beta One", "Store B", "salary", salary=6000)
    _mk("E-C1", "Gamma Hourly", "Store C", "hourly", salary=0, hourly_rate=25)
    return True


# ===========================================================================
# 0. CONTROLS — state every precondition these tests depend on, so that a change
#    to the role matrix or the schema fails HERE rather than making a regression
#    silently vacuous.
# ===========================================================================
def test_0a_pay_type_is_unvalidated_so_a_denylist_would_leak():
    """The justification for the allow-list, asserted rather than asserted-in-prose.

    If someone later adds an enum/validator to `pay_type`, this test fails and the
    allow-list can be revisited deliberately. Until then, any deny-list is porous.
    """
    for i, weird in enumerate(["Hourly", "HOURLY", " hourly ", "commission", "contract"]):
        e = _mk(f"E-W{i}", f"Weird {i}", "Store A", weird, salary=999)
        assert e["pay_type"] == weird, (
            f"pay_type {weird!r} was normalized or rejected on write; if the schema "
            f"now validates pay_type, revisit the allow-list rationale")
    # clean up so these do not perturb the Store A scenarios below
    for i in range(5):
        assert client.post(f"/api/employees/E-W{i}/deactivate",
                           headers=_h(OWNER)).status_code == 200


def test_0b_the_role_matrix_is_what_these_tests_assume():
    assert P.can("branch_manager", "run_payroll")
    assert not P.can("branch_manager", "view_payroll"), (
        "branch_manager gained view_payroll — test 8 no longer tests a disclosure "
        "boundary and the UX-B masking requirement has changed meaning")
    assert P.can("owner", "run_payroll") and P.can("owner", "view_payroll")


def test_0c_the_supported_path_still_works():
    """The guard must not have become 'refuse everything'.

    A containment that blocks the salaried case too would make every other test in
    this file pass for the wrong reason.
    """
    before = _counts()
    r = client.post("/api/payroll/finalize", headers=_h(OWNER),
                    params={"start": "2031-09-01", "end": "2031-09-30", "branch": "Store A"})
    assert r.status_code == 200, f"an all-salaried single branch must still finalize: {r.text}"
    after = _counts()
    assert after["ledger"] == before["ledger"] + 1
    assert after["payroll_runs"] == before["payroll_runs"] + 1


# ===========================================================================
# 1. BF-PR-01 — hourly pay must never be computed, presented or posted as $0.
# ===========================================================================
def test_1_finalize_refuses_a_scope_containing_an_hourly_employee():
    before = _counts()
    r = client.post("/api/payroll/finalize", headers=_h(OWNER),
                    params={"start": START, "end": END, "branch": "Store C"})
    assert r.status_code == 409, (
        f"finalize accepted a scope it cannot compute: {r.status_code} {r.text}")

    # The refusal must state HOW it concluded — a generic 'blocked' is not diagnosable.
    detail = r.json().get("detail", "")
    assert "E-C1" in detail, f"refusal does not name the blocking employee: {detail!r}"

    after = _counts()
    assert after == before, (
        f"a refused finalize changed durable state: {before} -> {after}. Ledger, "
        f"payroll_runs and audit_log must be untouched by construction.")


def test_2_the_read_marks_hourly_unsupported_and_omits_the_figure():
    """`0` must not appear as this employee's pay, and no total may be published
    that silently excludes them."""
    r = client.get("/api/payroll", headers=_h(OWNER),
                   params={"start": START, "end": END, "branch": "Store C"})
    assert r.status_code == 200, r.text
    body = r.json()

    # Located by id OR name: at the unfixed base the rows carry no `id` at all, and a
    # bare `next(...)` raised StopIteration — a red that says nothing about the
    # defect. Match on either key so the failure below is the substantive one.
    row = next((x for x in body["rows"]
                if x.get("id") == "E-C1" or x.get("name") == "Gamma Hourly"), None)
    assert row is not None, f"the hourly employee is absent from the payroll read: {body}"
    assert row.get("status") == "unsupported", f"hourly row not marked unsupported: {row}"
    assert "gross" not in row and "net" not in row, (
        f"hourly row still carries a computed figure: {row}. Omission is the "
        f"requirement; a zero is a claim that they earned nothing.")

    # No published total. A partial total is the exact hazard ERP-Accounting-Auditor
    # named: a mixed branch posts a NONZERO aggregate that silently omits the hourly
    # part, which no later scan can distinguish from a correct one.
    assert "gross" not in body and "total_cost" not in body, (
        f"a total was published over an incomputable roster: {body}")
    assert body.get("status") == "unsupported"


def test_3_the_guard_is_an_allowlist_not_a_denylist():
    """THE test that a deny-list implementation fails.

    Each of these normalizes to something other than `salary` and is uncomputable in
    exactly the same way as `hourly`, but none of them equals the string `"hourly"`.
    A `pay_type == "hourly"` check passes tests 1 and 2 and fails every case here.
    """
    for i, weird in enumerate(["Hourly", "HOURLY", " hourly ", "commission", ""]):
        eid = f"E-D{i}"
        _mk(eid, f"Deny {i}", "Store B", weird, salary=999)
        try:
            before = _counts()
            r = client.post("/api/payroll/finalize", headers=_h(OWNER),
                            params={"start": f"2031-0{i + 1}-01", "end": f"2031-0{i + 1}-28",
                                    "branch": "Store B"})
            assert r.status_code == 409, (
                f"pay_type {weird!r} was computed as if it were salary "
                f"({r.status_code}) — the guard is behaving as a deny-list")
            assert _counts() == before, f"pay_type {weird!r}: refused but wrote state"
        finally:
            # In a `finally` deliberately: the first draft cleaned up only on the
            # success path, so one failure here left an active blocked employee in
            # Store B and test 4 then failed for a reason that had nothing to do
            # with test 4. A fixture leak reads as a second defect.
            assert client.post(f"/api/employees/{eid}/deactivate",
                               headers=_h(OWNER)).status_code == 200


def test_4_deactivated_hourly_staff_do_not_block_the_supported_path():
    """Scope is ACTIVE employees. An hourly leaver must not permanently freeze
    payroll for a branch — that would be a containment that outgrows its cause."""
    _mk("E-B9", "Beta Gone", "Store B", "hourly", hourly_rate=40)
    assert client.post("/api/employees/E-B9/deactivate",
                       headers=_h(OWNER)).status_code == 200
    r = client.post("/api/payroll/finalize", headers=_h(OWNER),
                    params={"start": "2031-10-01", "end": "2031-10-31", "branch": "Store B"})
    assert r.status_code == 200, f"an inactive hourly employee blocked payroll: {r.text}"


# ===========================================================================
# 2. BF-PR-02 — one explicit branch, computed and persisted as the same branch.
# ===========================================================================
@pytest.mark.parametrize("params", [
    {"start": START, "end": END},                          # omitted -> defaulted "all"
    {"start": START, "end": END, "branch": "all"},          # explicit "all"
    {"start": START, "end": END, "branch": ""},             # empty
    {"start": START, "end": END, "branch": "ALL"},          # casing
    {"start": START, "end": END, "branch": " all "},        # whitespace
])
def test_5_finalize_refuses_any_non_specific_branch(params):
    """Refusal must precede every write, and must not be reachable around by
    spelling. `all` was the DEFAULT, so this was the ordinary path, not an edge."""
    before = _counts()
    r = client.post("/api/payroll/finalize", headers=_h(OWNER), params=params)
    assert r.status_code == 400, (
        f"{params} was accepted ({r.status_code}); payroll would be computed across "
        f"branches and persisted under one of them")
    assert _counts() == before, f"{params}: refused but wrote durable state"


def test_6_a_multi_branch_owner_cannot_post_combined_money_under_one_branch():
    """The QA reproduction, asserted as the fixed behaviour.

    At `3eb1ccd`: finalize(all) posted Store A / 9000 (carrying A+B+C), then an
    explicit finalize(Store B) for the SAME period was accepted and posted 6000 —
    durable consolidated payroll 15000 against a true salaried total of 9000.
    """
    period = {"start": "2031-04-01", "end": "2031-04-30"}
    before_runs = _runs()

    combined = client.post("/api/payroll/finalize", headers=_h(OWNER), params=period)
    assert combined.status_code == 400, "the combined posting is still reachable"
    assert _runs() == before_runs

    a = client.post("/api/payroll/finalize", headers=_h(OWNER),
                    params={**period, "branch": "Store A"})
    b = client.post("/api/payroll/finalize", headers=_h(OWNER),
                    params={**period, "branch": "Store B"})
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)

    new = [r for r in _runs() if r not in before_runs]
    by_branch = {r[0]: r[1] for r in new}
    assert set(by_branch) == {"Store A", "Store B"}, by_branch

    # THE EXPECTATION IS DERIVED, NOT HARD-CODED.
    #
    # The first draft asserted the literal 4500/6000 this module's own roster implies.
    # It failed at 7700/8600 because other employees exist in these branches, and a
    # magic number would have gone on failing — or worse, been "corrected" to 7700 —
    # for a reason unrelated to the defect. The invariant that actually matters is
    # that EACH PERSISTED ROW CARRIES ITS OWN BRANCH'S TOTAL, so ask the API what each
    # branch's total is and compare against that.
    #
    # This still goes red on the defect: at `3eb1ccd` the Store A row carried the
    # combined A+B+C figure, which is by construction not equal to Store A's own.
    expected = {}
    for b in ("Store A", "Store B"):
        r = client.get("/api/payroll", headers=_h(OWNER), params={**period, "branch": b})
        assert r.status_code == 200, r.text
        expected[b] = float(r.json()["gross"])

    assert by_branch == expected, (
        f"persisted payroll does not match each branch's own computed total: "
        f"persisted {by_branch} vs computed {expected} — a branch was mis-attributed")
    assert sum(by_branch.values()) == sum(expected.values()), (
        f"consolidated payroll {sum(by_branch.values())} != the true roster total "
        f"{sum(expected.values())}; a branch was double-counted")
    # And the whole point of BF-PR-02: no single row may carry more than its branch.
    for b, amount in by_branch.items():
        assert amount == expected[b], (b, amount, expected[b])


def test_7_an_explicit_unauthorized_branch_is_still_403_not_400():
    """The BF-PR-02 refusal must not swallow the authorization refusal.

    A 400 'be specific' returned for a branch the caller may not touch would be a
    weaker, and differently-shaped, answer than the 403 that authorization owes.
    """
    r = client.post("/api/payroll/finalize", headers=_h(BM),
                    params={"start": START, "end": END, "branch": "Store C"})
    assert r.status_code == 403, (
        f"branch_manager is assigned Store A/B only; Store C must be 403, got "
        f"{r.status_code} {r.text}")


# ===========================================================================
# 3. HIGH-07 / UX-B — the role boundary must survive these changes.
# ===========================================================================
def test_8_refusals_and_receipts_disclose_nothing_to_a_role_without_view_payroll():
    """New refusal messages are a NEW response surface. They name employee ids and
    pay types — both already readable by this role from `GET /api/employees` — and
    must never name money.
    """
    salaries = {3000.0, 1500.0, 6000.0, 4500.0}

    blocked = client.post("/api/payroll/finalize", headers=_h(BM),
                          params={"start": START, "end": END, "branch": "Store A"})
    # Store A is all-salaried, so this one succeeds; the receipt is the surface.
    assert blocked.status_code == 200, blocked.text
    body = blocked.json()
    assert body.get("ok") is True, "the caller must still get a receipt for the run"
    assert "rows" not in body, f"per-employee pay disclosed to branch_manager: {body}"
    leaked = salaries & {float(v) for v in _scalars(body)
                         if isinstance(v, (int, float)) and not isinstance(v, bool)}
    assert not leaked, f"finalize receipt disclosed payroll figures {leaked}: {body}"

    # ...and the read is still refused, so the fix did not widen the capability.
    assert client.get("/api/payroll", headers=_h(BM),
                      params={"start": START, "end": END, "branch": "Store A"}
                      ).status_code == 403

    # A pay-type refusal must not become a side channel either. U-bm cannot reach
    # Store C, so use a blocked employee on a branch they CAN reach.
    _mk("E-A9", "Alpha Hourly", "Store A", "hourly", hourly_rate=77)
    try:
        r = client.post("/api/payroll/finalize", headers=_h(BM),
                        params={"start": "2031-11-01", "end": "2031-11-30",
                                "branch": "Store A"})
        assert r.status_code == 409, r.text
        detail = str(r.json().get("detail", ""))
        for money_str in ("3000", "1500", "4500", "77"):
            assert money_str not in detail, (
                f"the refusal message leaked the figure {money_str} to a role "
                f"refused the payroll read: {detail!r}")
    finally:
        client.post("/api/employees/E-A9/deactivate", headers=_h(OWNER))


def test_9_the_frontend_does_not_coerce_a_withheld_total_to_zero():
    """SOURCE-LEVEL ONLY — this cannot prove what renders.

    The finalize receipt used to be coerced to zero when the figure was absent. The
    HIGH-07 fix deliberately OMITS the total for a caller without `view_payroll`, so
    that fallback turned 'you may not see this' into '$0.00' — and a branch manager
    was told a nonzero payroll had posted as zero. This asserts the defective
    expression is gone and a masked state exists. Whether the mask actually appears
    in a browser is ERP-UX-Reviewer's gate, not this one.

    THE DEFECTIVE EXPRESSION IS ASSEMBLED, NEVER WRITTEN LITERALLY, in this module or
    in `index.html`. A plain substring guard cannot tell code from prose: the first
    version of this test went red against a correctly-fixed file because the comment
    explaining the fix quoted the very expression it forbids. Keep it split.
    """
    needle = "r.gross" + "||" + "0"
    here = os.path.dirname(os.path.abspath(__file__))
    html = open(os.path.join(here, "..", "..", "index.html"), encoding="utf-8").read()
    assert needle not in html and needle.replace("||", " || ") not in html, (
        "the finalize receipt is still coerced to 0 when the figure is withheld")
    assert "PAY_MASK" in html, (
        "no masked state constant found; a withheld payroll total must render as a "
        "mask, never as a number")


def test_10_payroll_aggregates_are_masked_not_summed_to_zero():
    """SOURCE-LEVEL ONLY — the sibling of test 9, at the AGGREGATE sites.

    Masking every per-employee cell is not enough, and I shipped exactly that mistake.
    A withheld salary arrives as an ABSENT field, sums to 0, and the KPI cards and
    Totals row then render a confident "$0.00" over a roster the viewer may not see.
    ERP-UX-Reviewer measured the consequence in a browser: a `branch_manager` without
    `view_payroll` finalized a real **$2,067** pay run from a screen whose Gross, Net
    and Total Cost all read $0.00 — the same blind-finalize defect the per-cell mask
    was supposed to close, one aggregation level up.

    Every aggregate now goes through `payTotal`, which masks if ANY employee in the
    roster is withheld. The needles are assembled rather than written literally for
    the reason given in test 9: a substring guard cannot tell code from prose, and
    this docstring would otherwise trip it.

    This proves the call sites, not the rendering. The browser gate is
    ERP-UX-Reviewer's, and this test claims nothing about it.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    html = open(os.path.join(here, "..", "..", "index.html"), encoding="utf-8").read()

    assert "function payTotal(" in html, "the aggregate masking helper is gone"

    # The exact aggregate expressions that rendered a fabricated $0.
    for var in ("totG", "totNet", "tot.gross", "tot.ded", "tot.net"):
        bare = "money(" + var + ")"
        assert bare not in html, (
            f"{bare} renders a payroll aggregate without a mask gate. A withheld "
            f"salary sums to 0 here and is drawn as a real figure; route it through "
            f"payTotal(...) like the per-employee cells.")

    # ...and they are actually gated, not merely deleted.
    assert html.count("payTotal(") >= 8, (
        f"expected every payroll aggregate to call payTotal (7 call sites plus the "
        f"definition); found {html.count('payTotal(')}")
    # This assertion earned its place on its first run: it caught two aggregate sites
    # in the offline `payFinalize` that I had missed after fixing the KPI cards and
    # the totals row — a fourth and fifth instance of the same defect, in the same
    # change, after I had already been told the defect class twice.
