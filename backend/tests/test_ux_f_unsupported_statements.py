"""UX-F — do not offer double-entry statements a single-entry backend cannot produce.

ERP-UX-Reviewer: the Report Center advertised balance sheets and trial balances that
the backend cannot support.

This is confirmed at the source, not inferred from the UI: there is no chart of
accounts, no journal and no general ledger anywhere in the application or its
migrations. The Report Center nonetheless offered Balance Sheet, Trial Balance, Owner
Equity and Retained Earnings, and each RENDERED NUMBERS — assembled by rearranging a
flat transaction list into rows labelled "Assets", "Liabilities", "Debits",
"Credits".

Those figures are the dangerous kind: they cannot be wrong in a *detectable* way,
because nothing they claim to reconcile exists. A trial balance whose debits and
credits come from the same flat list will always look plausible and can never
disagree with anything. That is strictly worse than an error message, and it is the
same shape as the Financial Control Center certifying "Healthy" over figures it
recomputed with the helper that produced them.

REMOVED OR TRUTHFULLY CONSTRAINED, per the accepted acceptance criterion:

* Balance Sheet, Trial Balance, Owner Equity, Retained Earnings are registered as
  UNSUPPORTED. They stay VISIBLE, because deleting them would hide the limitation and
  silently shorten the roadmap, but they render an explanation instead of figures.
* General Ledger -> "Transaction Ledger" and Journal Entries -> "Transactions by
  Date". These two show real data; only the NAME asserted double-entry semantics the
  data does not have, so they are renamed rather than disabled. Disabling a report
  that works would be its own false statement.

WHAT THIS DOES NOT PROVE. Static properties of `index.html`. It does not render the
Report Center. ERP-UX-Reviewer owns the browser gate. It also does not claim the
accounting layer is planned, scoped or estimated — only that the product no longer
asserts it exists.
"""
import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Reports that require double-entry bookkeeping and therefore cannot be produced.
_UNSUPPORTABLE = ("balance", "trial", "equity", "retained")


def _html():
    with open(os.path.join(_ROOT, "index.html"), encoding="utf-8") as fh:
        return fh.read()


def _catalog(html):
    m = re.search(r"function repCatalog\(\)\{.*?\n\}", html, re.S)
    assert m, "repCatalog was not found"
    return m.group(0)


def test_double_entry_statements_are_not_registered_as_working_reports():
    """The substantive assertion: none of them may be wired to a builder that
    produces figures."""
    cat = _catalog(_html())
    still_live = []
    for rid in _UNSUPPORTABLE:
        if re.search(r"\bR\('Financial','%s'" % rid, cat):
            still_live.append(rid)
    assert not still_live, (
        f"these reports still render computed figures from a single-entry ledger: "
        f"{still_live}. Their debits and credits come from the same flat list, so "
        f"they can never disagree with anything and cannot be checked.")


def test_they_are_declared_unsupported_rather_than_quietly_dropped():
    """Removing them entirely would hide the limitation; the gap must stay legible."""
    cat = _catalog(_html())
    for rid in _UNSUPPORTABLE:
        assert re.search(r"\bU\('Financial','%s'" % rid, cat), (
            f"report '{rid}' is neither offered nor declared unsupported — it has "
            f"just disappeared, which hides the accounting gap instead of stating it")
    assert "function repUnsupported(" in _html(), \
        "the unsupported-report renderer is missing"


def test_the_unsupported_renderer_shows_no_figures():
    html = _html()
    m = re.search(r"function repUnsupported\(.*?\n\}", html, re.S)
    assert m, "repUnsupported was not found"
    body = m.group(0)
    assert "unsupported:true" in body.replace(" ", ""), \
        "the unsupported result is not flagged as such"
    # No KPI values and no table: an explanation, not a number.
    assert re.search(r"kpis:\s*\[\s*\]", body), "unsupported report still emits KPIs"
    assert re.search(r"table:\s*null", body), "unsupported report still emits a table"
    assert "not available" in body.lower(), \
        "the explanation does not plainly say the report is unavailable"


def test_reports_that_do_work_are_renamed_not_disabled():
    """The two transaction lists show real data; only their names overstated it.
    Disabling a working report would be its own false statement."""
    cat = _catalog(_html())
    assert "'Transaction Ledger'" in cat, \
        "the transaction list is still called a General Ledger"
    assert "'Transactions by Date'" in cat, \
        "the chronological list is still called Journal Entries"
    for gone in ("'General Ledger'", "'Journal Entries'"):
        assert gone not in cat, (
            f"{gone} still claims double-entry semantics the data does not have")
    # ...and they are still REGISTERED, i.e. not disabled as collateral damage.
    assert re.search(r"\bR\('Financial','gl'", cat), "the transaction ledger was disabled"
    assert re.search(r"\bR\('Financial','journal'", cat), "the date list was disabled"
