"""Frontend regression: a post-login authorization failure must not be reported as
a login failure.

Structural checks over the shipped index.html (same plain-file style as the repo's
e2e_*.js). They pin the fix for the cashier login bug: the boot sequence tolerates a
403 on a role-restricted dataset (e.g. inventory movements), and apiBoot() routes a
401 to re-login but a post-login 403 to a limited session — never to the generic
login-error screen.
"""
import os
import re

_HTML = os.path.join(os.path.dirname(__file__), "..", "..", "index.html")
with open(_HTML, encoding="utf-8") as f:
    HTML = f.read()


def test_safelist_helper_exists():
    assert "async function safeList(fn)" in HTML
    compact = re.sub(r"\s+", "", HTML)
    # it rethrows only a genuine auth failure (401) and resolves other errors to []
    assert "if(e&&e.status===401)throwe;return[];" in compact


def test_boot_datasets_are_fetched_through_safelist():
    # The movements fetch (the one that 403s for cashiers) MUST be resilient,
    # and the other optional datasets are wrapped defensively too.
    assert "safeList(API.movements)" in HTML
    for name in ("products", "sales", "expenses", "purchases", "employees"):
        assert f"safeList(API.{name})" in HTML, name


def test_apiBoot_distinguishes_auth_from_authorization_failure():
    m = re.search(r"async function apiBoot\(\)\{.*?\n  \}", HTML, re.S)
    assert m, "apiBoot not found"
    boot = m.group(0)
    compact = re.sub(r"\s+", "", boot)
    # 401 → re-login
    assert "e.status===401){apiLogout" in compact, "401 must route to re-login"
    # 403 → explicitly handled and NOT sent to the login screen
    assert "e.status===403" in boot, "no explicit 403 branch"
    # the 403 branch must not call loginScreen
    b403 = boot[boot.index("e.status===403"):]
    b403 = b403[:b403.index("else {")] if "else {" in b403 else b403
    assert "loginScreen" not in b403, "a post-login 403 must not show the login-error screen"


def test_login_error_screen_still_used_for_generic_errors():
    # network / unexpected errors still surface on the login screen (unchanged)
    assert "loginScreen(apiErr(e))" in HTML
