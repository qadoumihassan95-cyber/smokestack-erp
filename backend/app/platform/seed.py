"""PFS Platform seeding — BUSINESS-AGNOSTIC and idempotent.

It upserts whatever applications + modules are registered (shared platform
modules plus each application's own), then runs each application's optional
bootstrap hook. It contains no knowledge of any specific business — a founding
company, if any, is created by that application's own bootstrap.

Safe to run on every startup against the live database (purely additive).
"""
import json
import logging

from .. import models
from . import registry

log = logging.getLogger("pfs.seed")

# Per-application bootstrap failures — one company's failed provisioning must not
# abort the others or crash startup. Surfaced in /api/health.
_BOOTSTRAP_FAILURES = []


def bootstrap_failures():
    return list(_BOOTSTRAP_FAILURES)


def seed_applications(db):
    have = {a.key for a in db.query(models.Application).all()}
    for a in registry.applications():
        if a.key in have:
            continue
        db.add(models.Application(key=a.key, name=a.name, industry=a.industry,
                                  description=a.description, active=a.active))
    db.commit()


def seed_modules(db):
    have = {m.key for m in db.query(models.Module).all()}
    for key, (app_key, spec) in registry.all_module_specs().items():
        if key in have:
            continue
        db.add(models.Module(key=key, name=spec.name, category=spec.category,
                             application_key=app_key,
                             depends_on=json.dumps(spec.depends_on or []),
                             default_enabled=spec.default_enabled, is_beta=spec.beta,
                             version=registry.PLATFORM_VERSION))
    db.commit()


def run_bootstraps(db):
    """Let each application seed/adopt its own data (idempotent). Each app's
    bootstrap is ISOLATED: a failure is recorded and skipped so one application's
    (or company's) broken provisioning cannot abort the others or crash startup."""
    _BOOTSTRAP_FAILURES.clear()
    for a in registry.applications():
        if not a.bootstrap:
            continue
        try:
            a.bootstrap(db)
        except Exception as e:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:
                pass
            _BOOTSTRAP_FAILURES.append({"app": a.key, "error": repr(e)})
            log.error("bootstrap for application '%s' failed and was skipped: %r", a.key, e)
    return list(_BOOTSTRAP_FAILURES)


def seed_platform(db):
    seed_applications(db)
    seed_modules(db)
    run_bootstraps(db)
