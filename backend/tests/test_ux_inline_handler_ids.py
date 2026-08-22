"""UX-C class — no backend string identifier may reach an inline handler unquoted.

ERP-UX-Reviewer hit `ReferenceError: TR is not defined` clicking Approve on a
transfer. The cause is not the transfer feature:

    onclick="invAdvanceTransfer(${t.id})"      # t.id == "TR-000001"

The id is interpolated UNQUOTED into an inline handler, so the browser parses
`invAdvanceTransfer(TR-000001)` and evaluates the bare identifier `TR`. It worked
while ids were local counters (`++INV.seq`, numbers) and broke the moment the
backend supplied its `counters` strings — `TR-000001`, `PO-000001`, and the `S1`/`E1`
prefixes the ledger hydration stamps onto sales and expenses.

WHY THIS TEST IS A PREDICATE AND NOT A LIST OF SITES.
I first enumerated the sites by hand and found nine. Writing this predicate found
**eleven** — my list missed a ternary form (`invSaveProduct(${p?p.id:'null'})`) and a
method call with a second argument (`...splice(${i},1)`). That is the third time in
this candidate that an enumeration missed a member, and it is the reason the rule
here is "no unquoted interpolation anywhere in an inline handler" rather than "these
call sites are fixed".

WHY THE RULE IS UNIFORM RATHER THAN "QUOTE ONLY THE STRING IDS".
Which ids are numeric is a property of today's hydration, not of the code — product
ids are `i+1` right now and would break the instant they came from the backend.
A rule that depends on knowing which is which needs a maintained exception list, and
an exception list is the thing that gets forgotten. Everything is quoted; the four
finders that used `===` now compare with `String()` on both sides, which is the
convention `txById` in the same file already used; and the two index handlers coerce
with `Number()` at the boundary.

WHAT THIS CANNOT PROVE. It is a static property of `index.html`. It does not prove a
button works, and it never runs a browser. ERP-UX-Reviewer owns that gate and
ERP-QA's dynamic pass exercises the real identifiers. This test claims exactly one
thing: no inline handler can receive an identifier the JS parser will read as a bare
name.
"""
import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_INDEX = os.path.join(_ROOT, "index.html")

# An inline handler attribute containing a `${...}` that sits immediately after `(`
# or `,` with no opening quote — i.e. the interpolated value is parsed as JS source
# rather than passed as a string.
_UNQUOTED = re.compile(r'\son[a-z]+="[^"]*?[(,]\s*\$\{[^}]*\}')

# The same shape, correctly quoted. Used to prove the predicate is not vacuous.
_QUOTED = re.compile(r"""\son[a-z]+="[^"]*?[(,]\s*'\$\{[^}]*\}'""")


def _html():
    with open(_INDEX, encoding="utf-8") as fh:
        return fh.read()


def test_the_predicate_is_not_vacuous():
    """If the regex stopped matching anything at all it would pass forever.

    Assert it still finds the many CORRECTLY quoted handlers, so a change to the
    markup style fails here loudly instead of silently disarming the real check.
    """
    quoted = _QUOTED.findall(_html())
    # Floor set BELOW the count on the unfixed parent (27) on purpose. Calibrating it
    # to the fixed tree (36) would make this meta-guard go red on the parent for a
    # reason that has nothing to do with the defect — the substantive test below is
    # what must distinguish fixed from unfixed. A guard that reports a false FAIL
    # costs the same credibility as one that reports a false PASS.
    assert len(quoted) >= 25, (
        f"the handler predicate matched only {len(quoted)} quoted interpolations; "
        f"if the template style changed, fix this regex — do not delete it, because "
        f"the check below would then pass over anything")


def test_no_inline_handler_interpolates_an_unquoted_identifier():
    """THE assertion. One failure here is a runtime ReferenceError in a browser."""
    bad = [m.group(0).strip() for m in _UNQUOTED.finditer(_html())]
    assert not bad, (
        "these inline handlers interpolate a value unquoted, so any non-numeric id "
        "is parsed as a bare JavaScript identifier and throws ReferenceError "
        f"(this is the `TR is not defined` defect): {bad}")


def test_the_identifier_finders_compare_as_strings():
    """The other half of the fix, and the half that fails silently.

    Quoting the call site without relaxing the lookup turns a hard ReferenceError
    into a quiet no-op: `find(x => x.id === id)` never matches `'7'` against `7`, so
    the button stops throwing and also stops doing anything. That is worse than the
    crash, because nothing reports it.
    """
    html = _html()
    for fn in ("invDoAdjust", "invDoAddStock", "invAdvanceTransfer",
               "invCancelTransfer", "invSaveProduct"):
        m = re.search(re.escape("function " + fn + "(id){") + r".{0,160}", html, re.S)
        assert m, f"{fn} not found — was it renamed?"
        body = m.group(0)
        assert "x.id===id" not in body.replace(" ", ""), (
            f"{fn} still matches its id with strict === after the call site was "
            f"quoted; it will silently find nothing instead of throwing. Compare "
            f"with String() on both sides, as txById already does.")


def test_transfer_rows_carry_a_real_reference():
    """UX-D. The transfers table renders `t.ref`; API hydration set only `id`, so
    every row displayed the literal string "undefined". The backend id IS the human
    reference (`counters.TRANSFER` mints `TR-000001`), so this is a carry-through,
    not an invented value.
    """
    html = _html()
    m = re.search(r"INV\.transfers\s*=\s*tr\.map\(function\(t\)\s*\{\s*return\s*\{([^}]*)\}", html)
    assert m, "the transfers hydration block was not found — was it restructured?"
    fields = m.group(1)
    assert "ref:" in fields, (
        f"transfer hydration does not set `ref`, but the table renders it — rows "
        f"will show 'undefined': {fields.strip()[:200]}")
    assert re.search(r"ref:\s*t\.id\b", fields), (
        f"`ref` must carry the authoritative backend id, not a locally invented "
        f"value: {fields.strip()[:200]}")
