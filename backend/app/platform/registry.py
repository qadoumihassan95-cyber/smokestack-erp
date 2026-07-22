"""PFS Platform registry — BUSINESS-AGNOSTIC.

The platform knows nothing about any specific business. It only provides:
  * the data types an application uses to describe itself (AppDescriptor, ModuleSpec),
  * a registration API that applications call to plug themselves in,
  * a small set of cross-cutting SHARED modules (generic platform capabilities
    with no business rules — dashboard shell, settings, notifications, the
    Telegram transport, team chat, etc.).

All business-specific logic (an application's own modules, its identity, its
founding-company bootstrap) lives inside that application under app/apps/, and
is contributed here purely by registration. Adding a new ERP never requires a
change to the platform.
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional

PLATFORM_VERSION = "2.0.0"


@dataclass
class ModuleSpec:
    """A capability an application exposes. `depends_on` lists other module keys."""
    key: str
    name: str
    category: str
    depends_on: List[str] = field(default_factory=list)
    default_enabled: bool = True
    beta: bool = False


@dataclass
class AppDescriptor:
    """How an ERP application registers itself with the platform.

    `bootstrap(db)` is an optional, idempotent hook the application provides to
    seed/adopt its own data (e.g. adopt pre-existing rows as a founding company).
    The platform calls it but never contains its logic.
    """
    key: str
    name: str
    industry: str = ""
    description: str = ""
    active: bool = True
    modules: List[ModuleSpec] = field(default_factory=list)
    bootstrap: Optional[Callable] = None


# Cross-cutting modules the platform offers to every application. These are
# infrastructure capabilities — they contain no business rules and are therefore
# safe to live at the platform layer. `application_key` for these is "core".
SHARED_MODULES: List[ModuleSpec] = [
    ModuleSpec("dashboard", "Dashboard", "Core Platform"),
    ModuleSpec("settings", "Settings", "Settings"),
    ModuleSpec("audit", "Audit Log", "Core Platform"),
    ModuleSpec("notifications", "Notifications", "Notifications"),
    ModuleSpec("reports", "Reports", "Reports"),
    ModuleSpec("telegram", "Telegram Bot", "Telegram Bot"),
    ModuleSpec("reminders", "Reminders", "Notifications",
               depends_on=["telegram", "notifications"]),
    ModuleSpec("team_chat", "Team Chat", "Team Chat", depends_on=["notifications"]),
    ModuleSpec("assistant", "Assistant", "Dashboard"),
]

# Filled at runtime by application self-registration. Empty until apps load.
_REGISTRY: dict = {}


def register_application(app: AppDescriptor) -> AppDescriptor:
    _REGISTRY[app.key] = app
    return app


def applications() -> List[AppDescriptor]:
    return list(_REGISTRY.values())


def get_application(key: str) -> Optional[AppDescriptor]:
    return _REGISTRY.get(key)


def shared_modules() -> List[ModuleSpec]:
    return list(SHARED_MODULES)


def all_module_specs() -> dict:
    """key -> (application_key, ModuleSpec). Shared modules first (application
    'core'), then each registered application's own modules."""
    out: dict = {}
    for m in SHARED_MODULES:
        out[m.key] = ("core", m)
    for a in _REGISTRY.values():
        for m in a.modules:
            out[m.key] = (a.key, m)
    return out


def reset():  # test helper
    _REGISTRY.clear()
