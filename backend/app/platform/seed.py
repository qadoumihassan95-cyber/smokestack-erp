"""PFS Platform seeding — idempotent. Runs on every startup.

It (1) upserts the applications + modules from the code manifest, and (2) makes
sure the existing SmokeStack business exists as **Company #1** with all modules
enabled and a lifetime subscription. Purely additive: it never modifies tenant
data, so it is safe to run against the live production database on every boot.
"""
import json

from .. import models
from . import registry

SMOKESTACK_SLUG = "smokestack"
SMOKESTACK_OWNER = "U-owner"


def seed_applications(db):
    have = {a.key for a in db.query(models.Application).all()}
    for a in registry.APPLICATIONS:
        if a["key"] in have:
            continue
        db.add(models.Application(key=a["key"], name=a["name"], industry=a.get("industry"),
                                  description=a.get("description"), active=bool(a.get("active", True))))
    db.commit()


def seed_modules(db):
    have = {m.key for m in db.query(models.Module).all()}
    for m in registry.MODULES:
        if m["key"] in have:
            continue
        db.add(models.Module(key=m["key"], name=m["name"], category=m["category"],
                             application_key=m.get("application", "core"),
                             depends_on=json.dumps(m.get("depends_on") or []),
                             default_enabled=bool(m.get("default_enabled", True)),
                             is_beta=bool(m.get("beta", False)),
                             version=registry.PLATFORM_VERSION))
    db.commit()


def ensure_company_one(db):
    """The existing SmokeStack business as Company #1 — created once, never altered."""
    c = db.query(models.Company).filter(models.Company.slug == SMOKESTACK_SLUG).first()
    if not c:
        c = models.Company(name="SmokeStack", slug=SMOKESTACK_SLUG,
                           industry="Smoke & Vape Retail", application_key="smoke_shop",
                           owner_user_id=SMOKESTACK_OWNER, status="active",
                           version=registry.PLATFORM_VERSION)
        db.add(c)
        db.commit()
        db.refresh(c)
    # every module enabled for the founding company
    existing = {cm.module_key for cm in
                db.query(models.CompanyModule).filter(models.CompanyModule.company_id == c.id).all()}
    for m in registry.MODULES:
        if m["key"] in existing:
            continue
        db.add(models.CompanyModule(company_id=c.id, module_key=m["key"],
                                    enabled=bool(m.get("default_enabled", True)), source="global"))
    # a lifetime subscription so the founding company is never gated
    if not db.query(models.Subscription).filter(models.Subscription.company_id == c.id).first():
        db.add(models.Subscription(company_id=c.id, plan="lifetime", status="active"))
    db.commit()
    return c


def seed_platform(db):
    """Full idempotent platform seed. Safe on every boot, tenant data untouched."""
    seed_applications(db)
    seed_modules(db)
    return ensure_company_one(db)
