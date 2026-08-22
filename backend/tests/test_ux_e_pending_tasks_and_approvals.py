"""UX-E / UX-C — Pending Tasks must reflect real records, and approvals must be reachable.

ERP-UX-Reviewer: the dashboard's Pending Tasks panel was fabricated, and the client
made no approval API calls at all.

The panel rendered four hard-coded sentences — an invented cash figure, "Store C
hasn't reported today", "Payroll due in 3 days", "Sales-tax filing in 12 days" — none
of which came from any record. A fabricated task list is worse than an empty one: it
is indistinguishable from a real one, so it cannot be checked, and it teaches the
reader to ignore the panel that is supposed to be the one place urgent work appears.

The approval contract already existed and was complete on the server —
`GET /api/approvals`, `POST /api/approvals/{id}/approve|reject`, with `_decide`
moving stock for a transfer and setting purchase status inside ONE transaction with
its audit row. The client simply never called any of it. So this is a missing caller,
not a missing feature, which is why the fix is small.

THE ACTION BUTTONS USE DATA ATTRIBUTES, NOT INLINE HANDLERS. Approval ids are backend
strings (`APR-000001`), and an unquoted inline-handler argument is exactly what
produced `ReferenceError: TR is not defined` (see test_ux_inline_handler_ids). A
delegated listener reading `data-appr-ok` / `data-appr-no` cannot express that defect
at all. The existing attendance approvals card already used this pattern; the new UI
follows it rather than the inline style.

THE DECISION IS THE SERVER'S. The handler calls the API and then RE-HYDRATES rather
than patching local state: a rejected purchase must leave the pending list and stay
out of `LEDGER`, and an approved transfer has moved stock only the server knows
about. Guessing either locally is how displayed and stored numbers drift apart —
the same failure that produced a `$0` payroll display over a real posting.

WHAT THIS DOES NOT PROVE. Static properties of `index.html`. It does not click a
button, does not prove the panel renders, and does not prove a decision reaches the
database. ERP-UX-Reviewer owns the browser gate and ERP-QA drives the real
approve/reject state transition and its accounting side effects.
"""
import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Fabricated strings from the old panel. Any of them reappearing means invented
# urgency is back on the dashboard.
_FABRICATED = ("hasn't reported today", "Payroll due in 3 days",
               "Sales-tax filing in 12 days", "cash ready to deposit")


def _html():
    with open(os.path.join(_ROOT, "index.html"), encoding="utf-8") as fh:
        return fh.read()


def _code(html):
    """The file minus JS line comments, so assertions about CODE are not satisfied
    (or broken) by prose. This module documents the fabricated strings it forbids,
    which would otherwise make its own check fail — the trap that already caught the
    payroll guard once."""
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in html.splitlines())


def test_the_dashboard_does_not_fabricate_pending_work():
    code = _code(_html())
    present = [f for f in _FABRICATED if f in code]
    assert not present, (
        f"the Pending Tasks panel still renders invented items {present}. A "
        f"fabricated task is indistinguishable from a real one and cannot be "
        f"reconciled against any record.")


def test_pending_tasks_are_driven_by_real_approvals():
    html = _html()
    m = re.search(r"getElementById\('pending'\).{0,2600}", html, re.S)
    assert m, "the Pending Tasks host element is no longer rendered"
    block = m.group(0)
    assert "APPROVALS" in block, (
        "the panel does not read the approvals fetched from the server")
    assert re.search(r"(let|var|const)\s+APPROVALS", html), \
        "APPROVALS is used but never declared"
    assert "API.approvals()" in html, (
        "nothing calls GET /api/approvals — the contract exists on the server and "
        "the client must actually use it")


def test_the_empty_state_is_truthful():
    """An empty list is a real answer and must be said plainly, and a role that may
    not approve must not be shown 'nothing pending' — that is a different claim."""
    html = _html()
    m = re.search(r"getElementById\('pending'\).{0,2600}", html, re.S)
    block = m.group(0)
    assert "Nothing is awaiting your approval" in block, \
        "no truthful empty state for a user who has no pending approvals"
    assert "can('approve')" in block, (
        "the panel does not distinguish 'you may not approve' from 'nothing is "
        "pending'; showing the latter to an unentitled role is a false statement")


def test_approve_and_reject_are_both_reachable_for_purchase_and_transfer():
    """UX-C and UX-G both need REJECT, not only approve: without it an unapproved
    purchase can never be resolved either way."""
    html = _html()
    assert "API.approveReq" in html and "API.rejectReq" in html, \
        "approve and/or reject is not wired to the API"
    assert "/approve" in html and "/reject" in html, \
        "the approval endpoints are not called"
    assert "data-appr-ok" in html and "data-appr-no" in html, (
        "no approve/reject controls are rendered")


def test_approval_controls_do_not_use_inline_handlers():
    """The whole point of choosing data attributes here."""
    html = _html()
    assert not re.search(r'onclick="[^"]*approveReq', html), \
        "approve is wired through an inline handler; use the delegated listener"
    assert not re.search(r'onclick="[^"]*rejectReq', html), \
        "reject is wired through an inline handler; use the delegated listener"


def test_a_decision_rehydrates_instead_of_patching_local_state():
    html = _html()
    # Anchored on the listener's own selector. My first anchor was `data-appr-ok')`,
    # which never occurs — the id is read via a ternary, so the literal is followed
    # by `:` not `)`. The test failed on a correct tree: a guard that cannot find
    # what it is inspecting reports a false FAIL, which costs the same credibility
    # as a false PASS.
    m = re.search(r"closest\('\[data-appr-ok\]'\).{0,1600}", html, re.S)
    assert m, "the delegated approval listener was not found"
    handler = m.group(0)
    assert "apiRehydrate" in handler, (
        "the approval handler does not re-read server state after deciding; a "
        "locally patched view drifts from what was actually stored")
