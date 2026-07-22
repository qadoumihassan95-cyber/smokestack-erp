"""PFS Platform registry — the code manifest of applications and modules.

Adding a new ERP application or module is done HERE (declaratively). On startup
the manifest is idempotently upserted into the `applications`/`modules` tables,
so the Control Center automatically recognises it with no page or core changes —
this is the "future-ready / self-registering" requirement.

No AI, no runtime magic: a plain declarative list resolved deterministically.
"""

PLATFORM_VERSION = "2.0.0"          # bumped as the platform evolves

# The live SmokeStack business runs the `smoke_shop` application. The other
# applications are REGISTERED (so they appear in the Control Center and a company
# could be created on them) but marked inactive — their ERP functionality is
# added later by registration only, never by touching the core platform.
APPLICATIONS = [
    {"key": "smoke_shop", "name": "Smoke Shop ERP", "industry": "Smoke & Vape Retail",
     "description": "Accounting-first ERP for U.S. smoke shops (the original SmokeStack app).",
     "active": True},
    {"key": "retail", "name": "Retail ERP", "industry": "Retail", "active": False,
     "description": "General retail point-of-sale and inventory."},
    {"key": "restaurant", "name": "Restaurant ERP", "industry": "Food & Beverage", "active": False,
     "description": "Restaurant operations, menu and table management."},
    {"key": "warehouse", "name": "Warehouse ERP", "industry": "Logistics", "active": False,
     "description": "Warehouse, receiving and fulfilment."},
    {"key": "manufacturing", "name": "Manufacturing ERP", "industry": "Manufacturing", "active": False,
     "description": "Bill of materials, production and work orders."},
    {"key": "accounting", "name": "Accounting ERP", "industry": "Accounting", "active": False,
     "description": "Standalone books, ledgers and tax."},
    {"key": "auto_parts", "name": "Auto Parts ERP", "industry": "Automotive", "active": False,
     "description": "Auto-parts catalog, fitment and counter sales."},
    {"key": "clinic", "name": "Clinic ERP", "industry": "Healthcare", "active": False,
     "description": "Clinic scheduling, patients and billing."},
    {"key": "construction", "name": "Construction ERP", "industry": "Construction", "active": False,
     "description": "Projects, job costing and site management."},
]

# Modules the Control Center can enable/disable per company. `application` = the
# app a module belongs to; "core" = shared across every application. These map to
# the ERP features that already exist in SmokeStack today. Enabling/disabling is
# wired into the API + UI in Phase 2 — here we only register them.
MODULES = [
    # ---- Core platform (shared) ----
    {"key": "dashboard", "name": "Dashboard", "category": "Core Platform", "application": "core"},
    {"key": "settings", "name": "Settings", "category": "Settings", "application": "core"},
    {"key": "reports", "name": "Reports & Insights", "category": "Reports", "application": "core"},
    {"key": "notifications", "name": "Notifications", "category": "Notifications", "application": "core"},
    {"key": "audit", "name": "Audit Log", "category": "Core Platform", "application": "core"},
    # ---- Accounting ----
    {"key": "accounting", "name": "Accounting", "category": "Accounting", "application": "smoke_shop"},
    {"key": "sales", "name": "Daily Sales", "category": "Accounting", "application": "smoke_shop"},
    {"key": "expenses", "name": "Expenses", "category": "Accounting", "application": "smoke_shop"},
    {"key": "purchases", "name": "Purchases", "category": "Accounting", "application": "smoke_shop"},
    {"key": "taxes", "name": "Sales Tax", "category": "Accounting", "application": "smoke_shop"},
    {"key": "payroll", "name": "Payroll", "category": "Payroll", "application": "smoke_shop"},
    {"key": "control_center", "name": "Financial Control Center", "category": "Reports",
     "application": "smoke_shop"},
    # ---- Inventory ----
    {"key": "inventory", "name": "Inventory", "category": "Inventory", "application": "smoke_shop"},
    {"key": "transfers", "name": "Branch Transfers", "category": "Inventory", "application": "smoke_shop"},
    {"key": "barcode", "name": "Barcode Scanner", "category": "Inventory", "application": "smoke_shop"},
    # ---- CRM ----
    {"key": "customers", "name": "Customers", "category": "CRM", "application": "smoke_shop"},
    {"key": "suppliers", "name": "Suppliers", "category": "CRM", "application": "smoke_shop"},
    # ---- People / time ----
    {"key": "attendance", "name": "Attendance", "category": "Attendance", "application": "smoke_shop"},
    {"key": "work_hours", "name": "Work Hours & Schedule", "category": "Work Hours",
     "application": "smoke_shop"},
    {"key": "licenses", "name": "Licenses & Documents", "category": "Licenses", "application": "smoke_shop"},
    # ---- Communication (with a dependency: chat needs notifications) ----
    {"key": "telegram", "name": "Telegram Bot", "category": "Telegram Bot", "application": "core"},
    {"key": "reminders", "name": "Reminders", "category": "Notifications", "application": "core",
     "depends_on": ["telegram", "notifications"]},
    {"key": "team_chat", "name": "Team Chat", "category": "Team Chat", "application": "core",
     "depends_on": ["notifications"]},
    {"key": "assistant", "name": "Business Assistant", "category": "Dashboard", "application": "core"},
]


def module_map():
    return {m["key"]: m for m in MODULES}


def application_map():
    return {a["key"]: a for a in APPLICATIONS}
