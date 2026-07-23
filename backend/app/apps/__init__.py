"""Installed ERP applications — GENERIC application loader.

Every ERP application is a self-registering package/module in this directory.
Importing it runs its `register_application()` call, so the loader simply
discovers and imports every module here — it hardcodes NO specific business.
Adding a future ERP = drop a new package in app/apps/; the core, the PFS Control
Center, authentication and the tenant layer are never touched.

`load_apps()` is called once at startup (and by tests) before the platform seed.
After importing, it validates the registry:
  * every application manifest has a key + name,
  * no two applications share an application key,
  * no two modules share a module key (across apps and shared modules),
  * every module is owned by a registered application or by "core",
  * every module dependency refers to a known module.
Any violation raises at load time, so a broken application can never deploy.
"""
import importlib
import pkgutil

from ..platform import registry


def load_apps():
    """Discover and import every application module here (self-registration),
    then validate the resulting registry. Returns the registered applications."""
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{mod.name}")
    validate_registry()
    return registry.applications()


def discovered_app_modules():
    """Names of the application modules present on disk (no import side effects)."""
    return sorted(m.name for m in pkgutil.iter_modules(__path__)
                  if not m.name.startswith("_"))


def validate_registry():
    """Validate manifests, ownership, duplicate keys and dependencies. Raises
    ValueError on any inconsistency. Safe to call repeatedly (idempotent)."""
    apps = registry.applications()

    # 1) application manifests + unique application keys
    seen_apps = set()
    for a in apps:
        if not getattr(a, "key", None):
            raise ValueError("application manifest missing a key")
        if not getattr(a, "name", None):
            raise ValueError(f"application '{a.key}' manifest missing a name")
        if a.key in seen_apps:
            raise ValueError(f"duplicate application key: {a.key}")
        seen_apps.add(a.key)

    # 2) module ownership + unique module keys (shared 'core' first, then apps)
    owner = {}
    for m in registry.shared_modules():
        if m.key in owner:
            raise ValueError(f"duplicate module key: {m.key}")
        owner[m.key] = "core"
    for a in apps:
        for m in a.modules:
            if m.key in owner:
                raise ValueError(
                    f"duplicate module key '{m.key}' declared by '{a.key}' "
                    f"and '{owner[m.key]}'")
            owner[m.key] = a.key

    # 3) dependencies must reference known modules
    known = set(owner)
    for m in registry.shared_modules():
        for dep in m.depends_on:
            if dep not in known:
                raise ValueError(f"module '{m.key}' depends on unknown module '{dep}'")
    for a in apps:
        for m in a.modules:
            for dep in m.depends_on:
                if dep not in known:
                    raise ValueError(
                        f"module '{m.key}' (app '{a.key}') depends on unknown "
                        f"module '{dep}'")
    return True
