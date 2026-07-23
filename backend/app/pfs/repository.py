"""The ONLY place the Control Center reads or writes platform data.

This repository is the seam that keeps the Control Center portable: to move the
service to its own deployment that talks to the ERP over an API instead of the
shared DB, swap this one class for an API-backed implementation and nothing else
in the Control Center changes. It touches ONLY platform tables (platform_users,
companies, subscriptions, modules, company_modules, platform_audit) — never a
tenant's business tables.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .. import models  # the shared DB schema (shared-schema multi-tenancy)


class PlatformRepository:
    def __init__(self, db: Session):
        self.db = db

    # ---- Super Admins ---------------------------------------------------
    def get_super_admin(self, uid):
        return self.db.get(models.PlatformUser, uid)

    def get_super_admin_by_username(self, username):
        return (self.db.query(models.PlatformUser)
                .filter(models.PlatformUser.username == username).first())

    def touch_login(self, user):
        user.last_login = datetime.now(timezone.utc)
        self.db.commit()

    def create_super_admin(self, id, username, name, password_hash):
        u = models.PlatformUser(id=id, username=username, name=name,
                                password_hash=password_hash, active=True)
        self.db.add(u)
        self.db.commit()
        self.db.refresh(u)
        return u

    # ---- Companies (read surface for the scaffold overview) -------------
    def count_companies(self, status=None):
        q = self.db.query(models.Company)
        if status:
            q = q.filter(models.Company.status == status)
        return q.count()

    def list_companies(self):
        return self.db.query(models.Company).order_by(models.Company.id).all()

    # ---- Platform audit trail ------------------------------------------
    def audit(self, super_admin_id, action, entity="", ref="", company_id=None,
              detail="", ip=""):
        self.db.add(models.PlatformAudit(
            super_admin_id=super_admin_id, action=action, entity=entity,
            ref=str(ref), company_id=company_id, detail=detail, ip=ip))
        self.db.commit()
