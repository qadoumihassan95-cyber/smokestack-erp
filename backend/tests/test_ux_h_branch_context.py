"""UX-H — a new product's opening stock must not read as "vanished".

ERP-UX-Reviewer: post-create branch scoping is easy to misread — Store B opening
stock is perceived as missing when the Store A view is active.

The mechanism is a mismatch between two different branch selections that the UI
never relates to each other. Opening stock lands at the branch chosen INSIDE the
product form (`selBranch`), but the list the user is returned to is filtered by the
GLOBAL branch selector (`BRANCH`). When they differ, the new product appears with
quantity 0 and the reasonable reading is that the stock was lost.

Nothing was wrong with the data, which is exactly why this matters: the user's next
action is to re-enter stock that already exists, and now the count is wrong for real.
A confusing display that provokes a corrective action is a data-integrity problem
one step removed.

The fix is to say which branch received the stock, and — when the active view is not
that branch — to say plainly that this is why it shows 0 here.

WHAT THIS DOES NOT PROVE. A static property of `index.html`. It does not render the
toast. ERP-UX-Reviewer owns the browser gate.
"""
import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _html():
    with open(os.path.join(_ROOT, "index.html"), encoding="utf-8") as fh:
        return fh.read()


def _confirmation():
    m = re.search(r"toast\(BRANCH!=='all'.{0,700}", _html(), re.S)
    assert m, ("the post-create confirmation no longer distinguishes the receiving "
               "branch from the branch being viewed")
    return m.group(0)


def test_the_confirmation_names_the_branch_that_received_the_stock():
    body = _confirmation()
    assert "selBranch" in body, "the receiving branch is not named in the confirmation"
    assert "storeLabel(selBranch)" in body, (
        "the receiving branch is shown as a raw key rather than its display label")


def test_a_mismatch_is_stated_rather_than_left_to_be_inferred():
    body = _confirmation()
    assert "BRANCH!==selBranch" in body.replace(" ", ""), (
        "the confirmation does not compare the receiving branch against the branch "
        "currently being viewed, so a mismatch is silent")
    assert "shows 0 here" in body, (
        "the confirmation does not explain WHY the new product reads as empty in "
        "the current view; without that the user re-enters stock that already "
        "exists and makes the count wrong for real")
