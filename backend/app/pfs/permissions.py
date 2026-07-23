"""Control Center capability model — independent of the ERP's RBAC.

Super Admins operate the platform; these capabilities are platform-scoped and
share nothing with tenant permissions. Kept here (not in the ERP) so the
Control Center owns its own authorization when it becomes a separate service.
"""

CAP_SYSTEM_READ = "system.read"
CAP_COMPANIES_READ = "companies.read"
CAP_COMPANIES_MANAGE = "companies.manage"
CAP_IMPERSONATE = "companies.impersonate"
CAP_SUBSCRIPTIONS_MANAGE = "subscriptions.manage"
CAP_MODULES_MANAGE = "modules.manage"
CAP_FEATURES_MANAGE = "features.manage"
CAP_AUDIT_READ = "audit.read"
CAP_TELEGRAM_BROADCAST = "telegram.broadcast"

ALL_CAPS = {
    CAP_SYSTEM_READ, CAP_COMPANIES_READ, CAP_COMPANIES_MANAGE, CAP_IMPERSONATE,
    CAP_SUBSCRIPTIONS_MANAGE, CAP_MODULES_MANAGE, CAP_FEATURES_MANAGE,
    CAP_AUDIT_READ, CAP_TELEGRAM_BROADCAST,
}

# Role -> capabilities. `super_admin` is omnipotent; `support` is read-only.
# PlatformUser has no role column yet, so callers default to super_admin; adding
# roles later is an additive migration + a change here only.
ROLES = {
    "super_admin": set(ALL_CAPS),
    "support": {CAP_SYSTEM_READ, CAP_COMPANIES_READ, CAP_AUDIT_READ},
}


def capabilities_for(role):
    return ROLES.get(role or "super_admin", set())


def can(role, cap):
    return cap in capabilities_for(role)
