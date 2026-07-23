"""APPLICATION-AGNOSTIC ERP RUNTIME — enforced in CI.

The shared runtime provides only generic capabilities (auth, tenancy, module +
application registration, audit, notifications, telegram transport, background
jobs, shared repo/service interfaces). It must never depend on, import, or hard-
code a specific ERP application (Smoke Shop or any other). Applications self-
register through the generic loader; the runtime discovers them and knows nothing
about any one of them.

NOTE ON SCOPE: main.py and config.py still carry the legacy "SmokeStack" product
branding (title / default sqlite filename). Physically relocating that into the
smoke_shop application package is a later, dedicated refactor (agreed for Phase
1). They are therefore checked for the stronger structural rule — never importing
a specific application — but excluded from the cosmetic business-term scan until
that relocation happens. The genuinely generic runtime files are scanned fully.
"""
import ast
import os

_APP_DIR = os.path.join(os.path.dirname(__file__), "..", "app")

from app import apps as loader

# Files that must be COMPLETELY free of business identity today.
_CLEAN_RUNTIME = (
    ["database.py", "security.py", "tenancy.py", os.path.join("apps", "__init__.py")]
    + [os.path.join("platform", f) for f in os.listdir(os.path.join(_APP_DIR, "platform"))
       if f.endswith(".py")]
    + [os.path.join("pfs", f) for f in os.listdir(os.path.join(_APP_DIR, "pfs"))
       if f.endswith(".py")]
)

# Files that must never import a SPECIFIC application (structural rule), even if
# they still carry legacy branding strings.
_NO_APP_IMPORT = _CLEAN_RUNTIME + ["main.py", "config.py"]

_FORBIDDEN_TERMS = ("smoke", "vape", "tobacco", "cigarette")


def _read(rel):
    return open(os.path.join(_APP_DIR, rel), encoding="utf-8").read()


def _import_targets(src):
    """Yield dotted import target strings for `import x` and `from x import y`."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom):
            base = ("." * (node.level or 0)) + (node.module or "")
            for a in node.names:
                yield f"{base}::{a.name}"


# ------------------------------------------------- business-term cleanliness
def test_generic_runtime_files_contain_no_business_terms():
    offenders = []
    for rel in _CLEAN_RUNTIME:
        low = _read(rel).lower()
        for term in _FORBIDDEN_TERMS:
            if term in low:
                offenders.append(f"{rel}: '{term}'")
    assert not offenders, "business specifics leaked into the shared runtime: " + ", ".join(offenders)


# ------------------------------------------- never import a specific application
def test_shared_runtime_never_imports_a_specific_application():
    app_names = set(loader.discovered_app_modules())   # e.g. smoke_shop, catalog
    offenders = []
    for rel in _NO_APP_IMPORT:
        for tgt in _import_targets(_read(rel)):
            # normalise: strip the "::name" suffix for module matching
            mod, _, name = tgt.partition("::")
            # `import app.apps.smoke_shop` / `from ..apps.smoke_shop import x`
            for an in app_names:
                if f"apps.{an}" in mod:
                    offenders.append(f"{rel} -> {tgt}")
                # `from ..apps import smoke_shop`
                if mod.replace(".", "").endswith("apps") and name == an:
                    offenders.append(f"{rel} -> {tgt}")
    assert not offenders, ("shared runtime imported a specific application "
                           "(must go through the generic loader): " + ", ".join(offenders))


def test_the_loader_is_the_only_generic_seam_into_apps():
    # main.py may only reach applications via the generic loader entrypoints.
    targets = list(_import_targets(_read("main.py")))
    apps_imports = [t for t in targets if "apps" in t.split("::")[0].replace(".", "")]
    for t in apps_imports:
        _mod, _, name = t.partition("::")
        assert name in ("load_apps", "validate_registry", "discovered_app_modules",
                        "load_failures", ""), \
            f"main.py reaches into apps beyond the generic loader: {t}"
