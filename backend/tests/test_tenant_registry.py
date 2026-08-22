"""AA-C1-01 guard: the tenant registry is a hand-maintained list, so it will be
forgotten. This makes forgetting it a test failure instead of a silent
cross-company data fault.

`payroll_runs` was added to the schema in candidate 1 without being registered in
`tenancy.TENANT_TABLES`. Nothing failed. The table simply took PostgreSQL's
server-default `company_id = 1` while the ledger posting and audit row carried the
authenticated company, so a company-2 payroll produced a source document belonging
to company 1. Writing "remember to update TENANT_TABLES" in a comment would not
have caught it; this test does.
"""
from app import models
from app.tenancy import TENANT_TABLES, TENANT_EXEMPT


def _company_id_tables():
    out = {}
    for mapper in models.Base.registry.mappers:
        cls = mapper.class_
        table = getattr(cls, "__tablename__", None)
        if table and "company_id" in {c.name for c in mapper.columns}:
            out[table] = cls.__name__
    return out


def test_every_company_id_table_is_classified():
    """A table with a company_id column is either tenant-scoped or explicitly
    exempt. 'Neither' means it was forgotten, which is how AA-C1-01 happened."""
    tables = _company_id_tables()
    unclassified = sorted(t for t in tables if t not in TENANT_TABLES and t not in TENANT_EXEMPT)
    assert not unclassified, (
        "These tables carry company_id but are in neither tenancy.TENANT_TABLES nor "
        "tenancy.TENANT_EXEMPT, so they are unscoped by omission and will silently "
        "take the server-default company: " + ", ".join(unclassified)
    )


def test_payroll_runs_is_tenant_scoped():
    """The specific regression: the payroll source document must be tenant-scoped
    so it reconciles by company with the posting and the audit evidence."""
    assert "payroll_runs" in TENANT_TABLES
    assert "payroll_runs" not in TENANT_EXEMPT


def test_registry_sets_are_disjoint_and_real():
    """A table cannot be both scoped and exempt, and neither set may name a table
    that does not exist — a stale entry is a false sense of coverage."""
    overlap = TENANT_TABLES & TENANT_EXEMPT
    assert not overlap, f"tables listed as both scoped and exempt: {sorted(overlap)}"

    known = set(models.Base.metadata.tables)
    for name, s in (("TENANT_TABLES", TENANT_TABLES), ("TENANT_EXEMPT", TENANT_EXEMPT)):
        stale = sorted(t for t in s if t not in known)
        assert not stale, f"{name} names tables that no longer exist: {stale}"


def test_tenant_model_classes_includes_payroll_runs():
    """The scoping engine resolves classes from the registry, so registration must
    actually reach it — not just sit in a set."""
    from app.tenancy import tenant_model_classes
    assert models.PayrollRun in tenant_model_classes()
