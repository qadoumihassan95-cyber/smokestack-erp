"""Telegram capability catalogue.

This is deliberately NOT a second permission system. Each capability is just a
named entry point into the bot that declares which EXISTING ERP permissions it
requires; `permissions.PERMS` remains the only source of truth for what a role
may do. A capability is therefore available to an employee only when:

    the employee's ROLE grants every permission the capability requires
    AND the owner has not switched that capability off for this employee

The owner's per-employee toggles can only ever REMOVE capabilities. Switching a
capability on can never grant an employee something their ERP role does not
already allow — otherwise Telegram would become a privilege-escalation path
around the web app's RBAC.
"""

# key, label, required ERP permissions (from permissions.ALL_PERMS)
CAPABILITIES = [
    ("daily_sales",    "Daily Sales",               ["view", "create"]),
    ("expenses",       "Expenses",                  ["view", "create"]),
    ("purchases",      "Purchases",                 ["view", "create"]),
    ("inventory",      "Inventory",                 ["view"]),
    ("reports",        "Reports",                   ["view"]),
    ("dashboard",      "Dashboard",                 ["view"]),
    ("attendance",     "Attendance",                ["view"]),
    ("work_hours",     "Work Hours",                ["view"]),
    ("payroll",        "Payroll",                   ["view_payroll"]),
    ("control_center", "Financial Control Center",  ["view_all_branches"]),
    ("assistant",      "AI Business Assistant",     ["view"]),
    ("customers",      "Customers",                 ["view"]),
    ("suppliers",      "Suppliers",                 ["view"]),
    ("transfer",       "Branch Transfer",           ["transfer_stock"]),
    ("print",          "Print",                     ["print"]),
    ("export",         "Export",                    ["export"]),
    ("approvals",      "Approvals",                 ["approve"]),
]

CAP_KEYS = [k for k, _, _ in CAPABILITIES]
CAP_LABEL = {k: label for k, label, _ in CAPABILITIES}
CAP_PERMS = {k: perms for k, _, perms in CAPABILITIES}

DENIED_MESSAGE = "❌ You don't have permission to perform this action."


def role_allows(role: str, cap: str, P) -> bool:
    """Does the ERP role grant every permission this capability needs?"""
    perms = CAP_PERMS.get(cap)
    if perms is None:
        return False
    return all(P.can(role, p) for p in perms)


def effective(role: str, overrides: dict, P) -> dict:
    """The capability set a Telegram session actually gets.

    overrides is the owner's per-employee map {cap_key: bool}. A missing entry
    means "follow the role". An entry can only switch a capability OFF.
    """
    overrides = overrides or {}
    out = {}
    for cap in CAP_KEYS:
        allowed_by_role = role_allows(role, cap, P)
        if not allowed_by_role:
            out[cap] = False              # never escalate beyond the role
        else:
            out[cap] = bool(overrides.get(cap, True))
    return out


def describe(role: str, overrides: dict, P) -> list:
    """Rows for the admin UI: label, state, and WHY it is unavailable."""
    overrides = overrides or {}
    eff = effective(role, overrides, P)
    rows = []
    for cap in CAP_KEYS:
        by_role = role_allows(role, cap, P)
        rows.append({
            "key": cap,
            "label": CAP_LABEL[cap],
            "requires": CAP_PERMS[cap],
            "allowed_by_role": by_role,
            "enabled": eff[cap],
            # locked capabilities cannot be switched on: the role forbids them
            "locked": not by_role,
            "reason": ("" if by_role else
                       f"The {role.replace('_', ' ')} role does not grant: "
                       + ", ".join(p for p in CAP_PERMS[cap] if not P.can(role, p))),
        })
    return rows
