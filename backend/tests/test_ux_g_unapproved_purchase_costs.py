"""UX-G — an unapproved purchase must not reach costs, COGS, profit or cash.

ERP-UX-Reviewer: a pending purchase entered COGS immediately, with no reject path.

The cause is one line of hydration. Every purchase the API returned was pushed into
`LEDGER` regardless of `status`, and the `status` field was dropped on the way in:

    sx[2].forEach(function(x){ L.push({... type:'purchase', amount:x.amount ...}); });

`LEDGER` is what the client aggregates. More than eight separate sites sum
`type==='purchase'` into COGS, profit, cash, the P&L statement, the per-store
profit table and the vendor ledgers — and not one of them *could* have filtered,
because the status never arrived.

WHY THE FIX IS AT THE BOUNDARY AND NOT AT THE AGGREGATIONS.
Editing eight aggregation sites is the shape this candidate has already been burned
by three times: masked cells but not totals, three blueprints but not the fourth,
the transfer button but not the ledger. Each of those was a wrong aggregate closed
one call site at a time. Here the split happens ONCE, where the data enters: an
approved purchase is a financial event and goes into `LEDGER`; anything else goes
into `PUR_PENDING` and is invisible to every aggregation by construction. A ninth
aggregation added tomorrow is correct without anyone remembering this rule.

ALLOW-LIST, NOT A DENY-LIST — the same reasoning as BF-PR-01's pay types. The model
default is `pending_approval` and `_decide` writes `approved`/`rejected`, but nothing
constrains the column: no enum, no CHECK. Only the one value we can actually account
for is capitalised. A status nobody has invented yet stays out of COGS instead of
silently landing in it.

EXCLUDING IS NOT HIDING. `renderPur` unions both sources and labels the unapproved
rows, because an owner who cannot see a pending purchase cannot approve it. Trading a
wrong number for a lost document is not a fix.

WHAT THIS DOES NOT PROVE. Static properties of `index.html`. It does not run a
browser and does not prove the rendered COGS figure. ERP-UX-Reviewer owns that gate;
ERP-QA's dynamic pass drives the real approve/reject transition.
"""
import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _html():
    with open(os.path.join(_ROOT, "index.html"), encoding="utf-8") as fh:
        return fh.read()


def _hydration_block(html):
    m = re.search(r"sx\[2\]\.forEach\(function\(x\)\s*\{.*?\}\);", html, re.S)
    assert m, "the purchase hydration block was not found — was it restructured?"
    return m.group(0)


def test_purchase_hydration_gates_on_approval():
    block = _hydration_block(_html())
    assert "L.push" in block, "hydration no longer pushes anything into LEDGER"
    assert "'approved'" in block or '"approved"' in block, (
        f"purchase hydration does not test for an approved status, so unapproved "
        f"purchases still enter LEDGER and are summed into COGS:\n{block}")
    # The gate must be an equality against the ONE accountable value, not a
    # deny-list on the statuses that happen to exist today.
    assert not re.search(r"!==\s*['\"]pending_approval['\"]", block), (
        "the gate is a deny-list on 'pending_approval'; `status` has no enum or "
        "CHECK, so any other value would be capitalised into COGS. Allow only "
        "'approved'.")


def test_unapproved_purchases_are_kept_but_kept_out_of_the_ledger():
    html = _html()
    block = _hydration_block(html)
    assert "PUR_PENDING" in block, (
        f"unapproved purchases are not retained anywhere; excluding them from "
        f"LEDGER must not delete them, or they can never be approved:\n{block}")
    assert re.search(r"(let|var|const)\s+PUR_PENDING", html), \
        "PUR_PENDING is used but never declared"


def test_the_purchases_view_still_shows_unapproved_rows():
    """Excluding is not hiding — the owner has to be able to see what to approve."""
    m = re.search(r"function renderPur\(\)\{.*?rowify\('purTbl'", _html(), re.S)
    assert m, "renderPur was not found"
    body = m.group(0)
    assert "PUR_PENDING" in body, (
        "renderPur reads only LEDGER, so pending purchases are now invisible — that "
        "trades a wrong number for a lost document")
    assert "approval" in body.lower(), (
        "pending rows are shown but not labelled; an unlabelled row reads as a "
        "posted purchase")


def test_no_aggregation_was_patched_instead_of_the_boundary():
    """Guards the SHAPE of the fix, not just its effect.

    If someone later 'fixes' a regression by adding a status filter to one of the
    aggregation sites, that is the one-call-site-at-a-time pattern returning, and the
    boundary split has probably been weakened. The aggregations must stay ignorant of
    status — that is the entire point of splitting at hydration.
    """
    html = _html()
    for pattern in (r"type===['\"]purchase['\"]\s*&&\s*[a-z]*\.?status",
                    r"status===['\"]approved['\"]\s*&&\s*[a-z]*\.?type===['\"]purchase['\"]"):
        hits = re.findall(pattern, html)
        assert not hits, (
            f"an aggregation site is filtering purchases by status: {hits}. The "
            f"boundary split at hydration is what makes every aggregation correct; "
            f"re-introducing per-site filters means it has been weakened.")
