"""PFS Control Center — idempotent seed.

Creates the Platform Owner and **registers the existing SmokeStack fleet safely** (Step 8):
- ERP Product `smokestack` + its three Master environment definitions (no runtimes provisioned)
- an **Imported Legacy Release** (bootstrap exception — the current prod build predates the
  Master lifecycle; explicitly marked, never the normal publishing path — Decision 3)
- a **Customer-Production runtime** registering the existing SmokeStack production (read-only)
- a **Company #1 reference** + its **CustomerDeployment** (tenant_ref = company_id 1)
- an observed **Deployment** record

No SmokeStack code or data is touched; this only records metadata.
"""
from datetime import datetime

import models
from config import settings
from security import hash_pw


def seed(db):
    if not db.get(models.Operator, "OP-owner"):
        db.add(models.Operator(id="OP-owner", name="Platform Owner", email="owner@pfs.local",
                               password_hash=hash_pw(settings.seed_password),
                               platform_role="owner", status="active"))

    if not db.get(models.ErpProduct, "smokestack"):
        db.add(models.ErpProduct(id="smokestack", name="SmokeStack ERP",
                                 description="First ERP product on the PFS platform."))
        db.flush()
        for kind, dn in [("master_development", "Master Development"),
                         ("master_testing", "Master Testing"),
                         ("master_production", "Master Production")]:
            db.add(models.MasterEnvironment(erp_product_id="smokestack", kind=kind, display_name=dn))
    db.commit()

    rel = db.query(models.Release).filter_by(erp_product_id="smokestack", is_legacy_import=True).first()
    if not rel:
        rel = models.Release(
            erp_product_id="smokestack", version="1.0.0-legacy", source_sha="(imported)",
            build_identity="imported-legacy", source_master_runtime=None,
            status="imported_legacy", is_legacy_import=True,
            published_at=datetime.utcnow(), published_by="OP-owner",
            notes="Imported Legacy Release — current SmokeStack production predates the Master "
                  "lifecycle. Bootstrap exception only; NOT a normal publishing path (Decision 3).")
        db.add(rel)
        db.commit()

    rt = (db.query(models.Runtime)
          .filter_by(erp_product_id="smokestack", tier="customer",
                     environment_kind="customer_production").first())
    if not rt:
        rt = models.Runtime(
            erp_product_id="smokestack", tier="customer", environment_kind="customer_production",
            name="SmokeStack Customer Production", url="https://smokestack-erp.onrender.com",
            health_url="https://smokestack-api.onrender.com/api/health", status="registered",
            current_release_id=rel.id,
            notes="Existing SmokeStack production, registered read-only (metadata only).")
        db.add(rt)
        db.commit()

    cust = (db.query(models.CustomerRef)
            .filter_by(erp_product_id="smokestack", external_ref="1").first())
    if not cust:
        cust = models.CustomerRef(
            erp_product_id="smokestack", name="Company #1 (SmokeStack origin)",
            external_ref="1", status="active",
            notes="Reference only; the authoritative customer record lives in the ERP.")
        db.add(cust)
        db.commit()

    if not db.query(models.CustomerDeployment).filter_by(customer_ref_id=cust.id).first():
        db.add(models.CustomerDeployment(customer_ref_id=cust.id, release_id=rel.id,
                                         runtime_id=rt.id, tenant_ref="1", status="active"))
        db.commit()

    if not db.query(models.Deployment).filter_by(runtime_id=rt.id, release_id=rel.id).first():
        db.add(models.Deployment(runtime_id=rt.id, release_id=rel.id, kind="customer_deployment",
                                 status="observed", health_at_observe="unknown"))
        db.commit()
