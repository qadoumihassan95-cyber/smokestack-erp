"""Installed ERP applications.

Importing an app module registers it with the platform (self-registration).
`load_apps()` is called once at startup, before the platform seed runs, so the
registry reflects every installed application. The platform never imports these
modules directly — it only iterates whatever registered itself.
"""


def load_apps():
    # Each import triggers registration via register_application() at module load.
    from . import smoke_shop  # noqa: F401
    from . import catalog     # noqa: F401
