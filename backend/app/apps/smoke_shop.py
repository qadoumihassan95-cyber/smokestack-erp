"""Smoke Shop ERP application — the original SmokeStack ERP, expressed as a
platform application. ALL Smoke-Shop-specific knowledge lives here (its modules,
its identity, and the bootstrap that adopts the pre-existing production data as
its founding company). The platform layer contains none of this.
"""
from .. import models
from ..platform import registry
from ..platform.registry import AppDescriptor, ModuleSpec, register_application

# The business modules this ERP exposes (on top of the shared platform modules).
MODULES = [
    ModuleSpec("accounting", "Accounting", "Accounting"),
    ModuleSpec("sales", "Daily Sales", "Accounting"),
    ModuleSpec("expenses", "Expenses", "Accounting"),
    ModuleSpec("purchases", "Purchases", "Accounting"),
    ModuleSpec("taxes", "Sales Tax", "Accounting"),
    ModuleSpec("payroll", "Payroll", "Payroll"),
    ModuleSpec("control_center", "Financial Control Center", "Reports"),
    ModuleSpec("inventory", "Inventory", "Inventory"),
    ModuleSpec("transfers", "Branch Transfers", "Inventory"),
    ModuleSpec("barcode", "Barcode Scanner", "Inventory"),
    ModuleSpec("customers", "Customers", "CRM"),
    ModuleSpec("suppliers", "Suppliers", "CRM"),
    ModuleSpec("attendance", "Attendance", "Attendance"),
    ModuleSpec("work_hours", "Work Hours & Schedule", "Work Hours"),
    ModuleSpec("licenses", "Licenses & Documents", "Licenses"),
]

# Identity of this app's founding company (the existing production business).
_FOUNDING = {"name": "SmokeStack", "slug": "smokestack",
             "owner": "U-owner", "industry": "Smoke & Vape Retail"}


def bootstrap(db):
    """Adopt the pre-existing tenant data as this application's founding company.

    Idempotent and business-specific — owned by the app, not the platform. In the
    shared-schema model the existing rows are Company #1; this creates the Company
    record, enables the shared + Smoke-Shop modules, and grants a lifetime plan."""
    c = db.query(models.Company).filter(models.Company.slug == _FOUNDING["slug"]).first()
    if not c:
        c = models.Company(name=_FOUNDING["name"], slug=_FOUNDING["slug"],
                           industry=_FOUNDING["industry"], application_key="smoke_shop",
                           owner_user_id=_FOUNDING["owner"], status="active",
                           version=registry.PLATFORM_VERSION)
        db.add(c)
        db.commit()
        db.refresh(c)
    existing = {cm.module_key for cm in
                db.query(models.CompanyModule).filter(models.CompanyModule.company_id == c.id).all()}
    for m in [*registry.shared_modules(), *MODULES]:
        if m.key not in existing:
            db.add(models.CompanyModule(company_id=c.id, module_key=m.key,
                                        enabled=m.default_enabled, source="global"))
    if not db.query(models.Subscription).filter(models.Subscription.company_id == c.id).first():
        db.add(models.Subscription(company_id=c.id, plan="lifetime", status="active"))
    db.commit()
    return c


SMOKE_SHOP = AppDescriptor(
    key="smoke_shop", name="Smoke Shop ERP", industry="Smoke & Vape Retail",
    description="Accounting-first ERP for U.S. smoke shops (the original SmokeStack app).",
    active=True, modules=MODULES, bootstrap=bootstrap)

register_application(SMOKE_SHOP)
