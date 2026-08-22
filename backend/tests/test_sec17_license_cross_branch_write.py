"""SEC-17 regression — a branch-scoped role must not read, rewrite, relocate or
delete another branch's regulatory documents.

At `8868af8`, `licenses.update_license` guarded the branch the CALLER SUPPLIED and
never the branch the record is in:

```python
x = db.get(models.License, lid)
if body.branch:                          # <- only if supplied…
    S.assert_branch(user, db, body.branch)   # <- …and only the SUPPLIED branch
for f in (..., "branch", ...):           # <- and branch is writable
```

Omitting `branch` from the body therefore ran **no branch check at all**.
ERP-Security-Auditor executed it: `U-inv` (inventory_manager, Store A only) issued
`PUT /api/licenses/2` against a Store C record and the write **landed** — 1 leak in
23 attempts, the other 22 correctly refused.

Licences here are regulatory documents: tobacco retail permits, business licences,
fire inspections. A branch manager could alter another branch's permit number or
expiry date.

**This violates a rule the codebase wrote for itself.** `security.py:212`:
*"Authorization is never derived solely from a branch supplied by the requester."*
`hr.py` already obeys it with `assert_object_branch`. The fix is a port of an
implementation that already existed one file away, which is why tests 5 and 6 assert
the RULE and not just the two routes — a third route reintroducing the shape is the
actual risk.

TEST 4 IS THE LATENT SIBLING. `delete_license` had no branch check of any kind and
was unexploitable at `8868af8` only because `delete` happens to be held by owner and
admin alone, both all-branch. That is safe-by-current-config, not safe-by-construction:
granting `delete` to any branch-scoped role would have made it cross-branch DELETION
of compliance records with no code change and no failing test. The test grants exactly
that, so the property is asserted rather than the configuration being trusted.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"sec17_lic_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("SEED_ON_START", "true")
os.environ.setdefault("SEED_DEMO_DATA", "true")
os.environ.setdefault("JWT_SECRET", "sec17-secret-long-enough-for-tests")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")

import pytest                                        # noqa: E402
from fastapi.testclient import TestClient            # noqa: E402

from app.main import app                             # noqa: E402
from app import models, permissions as P, tenancy    # noqa: E402

with TestClient(app):
    pass
client = TestClient(app)

PW = "demo1234"
ATTACKER = "U-inv"          # inventory_manager, assigned Store A ONLY, holds `edit`
OWNER = "U-owner"           # all branches — the control
FAR = "Store C"             # a branch the attacker does not hold
NEAR = "Store A"            # the attacker's own branch


def _h(uid, pw=PW):
    r = client.post("/api/auth/login", data={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _mk_license(branch, number):
    """Create through the real endpoint as the owner, then return its id."""
    r = client.post("/api/licenses", headers=_h(OWNER), json={
        "name": f"Permit {number}", "doc_type": "tobacco_license", "branch": branch,
        "doc_number": number, "authority": "TX Comptroller",
        "issue_date": "2026-01-01", "expiry_date": "2027-01-01",
        "responsible": "Owner"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _row(lid):
    with tenancy.tenant_session(1) as db:
        return db.get(models.License, lid)


def _snapshot(lid):
    x = _row(lid)
    return None if x is None else (x.name, x.doc_number, x.branch, str(x.expiry_date))


@pytest.fixture(autouse=True)
def _restore_matrix():
    """Test 4 mutates the permission matrix deliberately; put it back whatever
    happens, or every later module inherits a role that can delete licences."""
    import copy
    before = copy.deepcopy(P.PERMS)
    yield
    P.PERMS.clear()
    P.PERMS.update(before)


# ===========================================================================
# 0. CONTROLS — the preconditions the finding depends on.
# ===========================================================================
def test_0_the_attacker_is_branch_scoped_and_holds_edit():
    assert P.can("inventory_manager", "edit"), "attacker cannot reach the route at all"
    assert not P.can("inventory_manager", "view_all_branches"), (
        "inventory_manager is no longer branch-scoped — this module cannot test a "
        "cross-branch write")
    with tenancy.tenant_session(1) as db:
        scope = [b.branch for b in db.query(models.UserBranch)
                 .filter(models.UserBranch.user_id == ATTACKER).all()]
    assert scope == [NEAR], f"fixture expects {ATTACKER} at {NEAR} only, got {scope}"


def test_1_the_attacker_can_still_edit_its_OWN_branchs_licence():
    """Without this, every refusal below could mean the route is simply broken."""
    lid = _mk_license(NEAR, "OWN-001")
    r = client.put(f"/api/licenses/{lid}", headers=_h(ATTACKER),
                   json={"name": "Permit OWN-001", "doc_type": "tobacco_license",
                         "doc_number": "OWN-001-EDITED", "expiry_date": "2028-01-01"})
    assert r.status_code == 200, f"the attacker cannot edit its own branch: {r.text}"
    assert _row(lid).doc_number == "OWN-001-EDITED"


# ===========================================================================
# 1. THE FINDING.
# ===========================================================================
def test_2_a_cross_branch_licence_write_is_refused_and_the_record_is_unchanged():
    """The auditor's exact attack: no `branch` in the body, so at 8868af8 no branch
    check ran at all and the write landed."""
    lid = _mk_license(FAR, "FAR-001")
    before = _snapshot(lid)

    r = client.put(f"/api/licenses/{lid}", headers=_h(ATTACKER),
                   json={"name": "Rewritten", "doc_type": "tobacco_license",
                         "doc_number": "C-999", "expiry_date": "2030-01-01"})

    assert r.status_code == 403, f"cross-branch licence write returned {r.status_code}"
    assert _snapshot(lid) == before, (
        f"the record changed despite the refusal: {before} -> {_snapshot(lid)}")


def test_3_a_licence_cannot_be_RELOCATED_out_of_another_branch():
    """`branch` is in the setattr allow-list, so the same call could move another
    branch's licence into the attacker's own — a supplied-branch check alone would
    have PASSED this, because the destination is legitimately theirs."""
    lid = _mk_license(FAR, "FAR-002")
    before = _snapshot(lid)

    r = client.put(f"/api/licenses/{lid}", headers=_h(ATTACKER),
                   json={"name": "Relocated", "doc_type": "tobacco_license",
                         "branch": NEAR, "doc_number": "C-998"})

    assert r.status_code == 403, (
        f"a licence was relocated out of {FAR} into the caller's own branch: "
        f"{r.status_code}")
    assert _row(lid).branch == FAR, f"branch moved to {_row(lid).branch}"
    assert _snapshot(lid) == before


def test_4_delete_is_guarded_by_construction_not_by_the_current_permission_matrix():
    """The latent sibling. `delete_license` had no branch check at all; it 403'd at
    `8868af8` only because `delete` is held by owner and admin, both all-branch.

    Granting `delete` to the branch-scoped role is exactly the one-line config change
    that would have made it exploitable with no code change and no failing test. Do
    that, and assert the route still refuses.
    """
    lid = _mk_license(FAR, "FAR-003")
    P.PERMS["inventory_manager"] = list(P.PERMS["inventory_manager"]) + ["delete"]
    assert P.can("inventory_manager", "delete"), "the precondition did not take effect"

    r = client.delete(f"/api/licenses/{lid}", headers=_h(ATTACKER))

    assert r.status_code == 403, (
        f"a branch-scoped role holding `delete` destroyed another branch's "
        f"compliance record: {r.status_code}")
    assert _row(lid) is not None, "the licence was deleted"

    # control: the same role CAN delete its own branch's licence, so the refusal
    # above is branch scope and not the permission grant failing to apply.
    own = _mk_license(NEAR, "OWN-003")
    ok = client.delete(f"/api/licenses/{own}", headers=_h(ATTACKER))
    assert ok.status_code == 200, f"the grant did not actually work: {ok.text}"
    assert _row(own) is None


def test_5_an_all_branch_role_is_unaffected():
    """The fix is scope, not a new prohibition."""
    lid = _mk_license(FAR, "FAR-004")
    r = client.put(f"/api/licenses/{lid}", headers=_h(OWNER),
                   json={"name": "Owner edit", "doc_type": "tobacco_license",
                         "doc_number": "C-997"})
    assert r.status_code == 200, r.text
    assert _row(lid).doc_number == "C-997"
    assert client.delete(f"/api/licenses/{lid}", headers=_h(OWNER)).status_code == 200


# ===========================================================================
# 2. The rule, not the two routes.
# ===========================================================================
def test_6_no_by_id_licence_mutation_authorizes_on_the_supplied_branch_alone():
    """`security.py` states the rule; this asserts it for this router.

    Written as a property over the module's source because the risk is a THIRD
    route reintroducing the shape, not these two regressing. Located by parsing
    rather than grepping, so the sentence stating the rule in a comment cannot
    satisfy or trip it.
    """
    import ast
    import inspect
    import re

    from app.routers import licenses as L

    src = inspect.getsource(L)
    tree = ast.parse(src)
    offenders = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        decs = [ast.get_source_segment(src, d) or "" for d in fn.decorator_list]
        route = next((d for d in decs
                      if any(v in d for v in (".put(", ".delete(", ".patch("))), None)
        if not route or "{" not in route:
            continue
        body = ast.get_source_segment(src, fn) or ""
        fetched = re.findall(r"(\w+)\s*=\s*db\.get\(models\.License", body)
        if not fetched:
            continue
        guards_object = any(
            re.search(rf"assert_object_branch\(\s*user\s*,\s*db\s*,\s*{v}\.branch", body)
            or re.search(rf"assert_branch\([^)]*\b{v}\.branch", body)
            for v in fetched)
        if not guards_object:
            offenders.append(fn.name)

    assert not offenders, (
        f"{offenders} fetch a licence by id and never authorize on the record's OWN "
        f"branch. security.py:212 — authorization is never derived solely from a "
        f"branch supplied by the requester.")
