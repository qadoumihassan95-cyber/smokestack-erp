"""BF-CC-01 — the Financial Control Center must be able to report a wrong number.

Three defect classes are covered:

  C1  Accounting checks recomputed each figure with the SAME helper that produced
      it, so a defect inside that helper moved both sides of the comparison and
      the check stayed green.
  C2  Four checks whose condition was the literal `True`.
  C3  The headline verdict was score-only, so a critical financial failure read
      "Attention needed" and adding passing checks made the report look healthier.

The tests below are structured so that each one FAILS on uncorrected candidate 2
(`8838963f`) and passes on the correction. That property is the point: a harness
that has never gone red is not evidence.

Design notes worth keeping:

* The AST guard parses source. It must not grep, because the word `_sum` appears
  in the very comments that explain the rule — a grep guard would fire on its own
  prose and would fall to any reformatting.
* Mutations are applied to the PRODUCTION aggregation only, never to the oracle.
  Patching both sides is exactly the defect being tested for.
* Fixture values are asserted exactly BEFORE any mutation, so a mutation cannot
  quietly corrupt the fixture and manufacture a pass.
"""
import ast
import os
import tempfile
from datetime import date, timedelta
from decimal import Decimal

import pytest

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_bfcc01_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("SEED_ON_START", "true")
os.environ.setdefault("JWT_SECRET", "bfcc01-test-secret-long-enough")

from fastapi.testclient import TestClient          # noqa: E402
from app.main import app                           # noqa: E402
from app.database import SessionLocal              # noqa: E402
from app import models                             # noqa: E402
from app import control_oracle as ORACLE           # noqa: E402
from app.routers import core as CORE               # noqa: E402
from app.routers import control as CTRL            # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORACLE_SRC = os.path.join(HERE, "app", "control_oracle.py")
CONTROL_SRC = os.path.join(HERE, "app", "routers", "control.py")

# Production aggregation helpers the oracle may never reach for.
_FORBIDDEN_CALLS = {"_sum", "_purchases_sum", "_costs_profit"}
_FORBIDDEN_IMPORTS = ("app.routers.core", "app.routers.control", ".routers.core", "core", "control")


# --------------------------------------------------------------------------
# C2 / independence — structural guards over source
# --------------------------------------------------------------------------

def test_oracle_does_not_import_production_aggregation():
    """Independence must be structural, not a review-time promise.

    Copying `_sum` under a new name would satisfy a human reviewer glancing at
    the diff. It does not satisfy this test.
    """
    tree = ast.parse(open(ORACLE_SRC).read())
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if any(mod == f or mod.endswith(f) for f in _FORBIDDEN_IMPORTS):
                bad.append(f"line {node.lineno}: from {mod} import ...")
            for alias in node.names:
                if alias.name in _FORBIDDEN_CALLS:
                    bad.append(f"line {node.lineno}: imports {alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name.endswith(f) for f in _FORBIDDEN_IMPORTS):
                    bad.append(f"line {node.lineno}: import {alias.name}")
    assert not bad, "oracle is not independent of production aggregation: " + "; ".join(bad)


def test_oracle_calls_no_production_helper():
    tree = ast.parse(open(ORACLE_SRC).read())
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name in _FORBIDDEN_CALLS:
                bad.append(f"line {node.lineno}: calls {name}()")
    assert not bad, "oracle calls production aggregation: " + "; ".join(bad)


def _literal_true_checks(path):
    """Every R.add() whose third positional arg is the literal True.

    Parsed, not grepped: one of the four original defects was a multiline call
    that a line-oriented pattern match missed entirely.
    """
    tree = ast.parse(open(path).read())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if getattr(fn, "attr", None) != "add":
            continue
        if len(node.args) < 3:
            continue
        cond = node.args[2]
        if isinstance(cond, ast.Constant) and cond.value is True:
            label = ast.literal_eval(node.args[1]) if isinstance(node.args[1], ast.Constant) else "?"
            found.append((node.lineno, label))
    return found


def test_no_check_has_an_unconditional_pass():
    """A check whose condition is `True` cannot fail, so it certifies nothing.

    On uncorrected candidate 2 this finds four: control.py lines 302, 338, 419
    and 423.
    """
    found = _literal_true_checks(CONTROL_SRC)
    assert not found, "unconditional pass conditions: " + "; ".join(
        f"line {ln}: {name!r}" for ln, name in found)


def test_ast_guard_detects_a_planted_unconditional_pass(tmp_path):
    """The guard must be able to report a defect — prove it on a known-bad file."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "R.add('M', 'single line', True, 'error', 'd')\n"
        "R.add('M',\n      'multi line',\n      True,\n      'error', 'd')\n"
    )
    found = _literal_true_checks(str(bad))
    names = sorted(n for _, n in found)
    assert names == ["multi line", "single line"], f"guard missed a case: {found}"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

_MARK = "BFCC01-PROBE"      # every row this module creates carries it


@pytest.fixture()
def seeded():
    """Known rows, asserted exactly before any mutation runs.

    This module shares one process-wide engine with every other test module, so
    it must not wipe tables it does not own and must remove everything it
    creates. An earlier draft deleted all ledger/purchase rows and left a
    payroll run behind; the leftover row surfaced as a critical finding inside
    `test_control.py::test_healthy_system_scores_high`, which was right to fail.

    Exact assertions are therefore made on the DELTA this fixture introduces,
    measured against the oracle before and after insertion. That keeps them
    exact without depending on the rest of the suite leaving the tables empty.
    """
    with TestClient(app):
        db = SessionLocal()
        try:
            brs = [b.name for b in db.query(models.Branch).all()]
            a, b = brs[0], brs[1]
            today = date.today()
            first = today.replace(day=1)
            prior = first - timedelta(days=1)

            rows = [
                models.Ledger(branch=a, type="sale", amount=Decimal("1000.10"),
                              tax=Decimal("80.01"), entry_date=today, company_id=1,
                              memo=_MARK),
                models.Ledger(branch=a, type="expense", amount=Decimal("200.02"),
                              tax=0, entry_date=today, company_id=1, memo=_MARK),
                models.Ledger(branch=b, type="sale", amount=Decimal("500.15"),
                              tax=Decimal("40.04"), entry_date=today, company_id=1,
                              memo=_MARK),
                # out-of-period row: must never reach the oracle for this month
                models.Ledger(branch=a, type="sale", amount=Decimal("7777.77"),
                              tax=0, entry_date=prior, company_id=1, memo=_MARK),
                # other-company row: must never reach a company-1 oracle
                models.Ledger(branch=a, type="sale", amount=Decimal("9999.99"),
                              tax=0, entry_date=today, company_id=2, memo=_MARK),
            ]

            # Baseline BEFORE our rows exist, so assertions can be exact deltas.
            base = {
                "sale": ORACLE.ledger_total(db, company_id=1, branches=[a, b],
                                            typ="sale", d0=first, d1=today),
                "tax": ORACLE.ledger_total(db, company_id=1, branches=[a, b],
                                           typ="sale", d0=first, d1=today, column="tax"),
                "expense": ORACLE.ledger_total(db, company_id=1, branches=[a, b],
                                               typ="expense", d0=first, d1=today),
                "purchases": ORACLE.purchases_total(db, company_id=1, branches=[a, b],
                                                    d0=first, d1=today),
                "sale_c2": ORACLE.ledger_total(db, company_id=2, branches=[a, b],
                                               typ="sale", d0=first, d1=today),
                "sale_wide": ORACLE.ledger_total(db, company_id=1, branches=[a, b],
                                                 typ="sale", d0=date(1970, 1, 1),
                                                 d1=date(2999, 1, 1)),
            }

            for r in rows:
                db.add(r)
            db.add(models.Purchase(id="P-bfcc", vendor=_MARK, branch=a,
                                   amount=Decimal("400.20"), status="approved",
                                   purchase_date=first, company_id=1))
            db.add(models.Purchase(id="P-rej", vendor=_MARK, branch=a,
                                   amount=Decimal("55.55"), status="rejected",
                                   purchase_date=first, company_id=1))
            db.commit()
            try:
                yield db, [a, b], first, today, base
            finally:
                # Remove exactly what this module created — nothing else.
                db.rollback()
                db.query(models.PayrollRun).filter(
                    models.PayrollRun.finalized_by == _MARK).delete(
                        synchronize_session=False)
                db.query(models.Ledger).filter(
                    models.Ledger.memo == _MARK).delete(synchronize_session=False)
                db.query(models.Purchase).filter(
                    models.Purchase.vendor == _MARK).delete(synchronize_session=False)
                db.commit()
        finally:
            db.close()


def test_fixture_values_are_exact_before_any_mutation(seeded):
    """Rule 2: if the fixture is not pinned, a mutation can corrupt it and pass."""
    db, brs, d0, d1, base = seeded
    got_sale = ORACLE.ledger_total(db, company_id=1, branches=brs, typ="sale", d0=d0, d1=d1)
    got_tax = ORACLE.ledger_total(db, company_id=1, branches=brs, typ="sale",
                                  d0=d0, d1=d1, column="tax")
    got_exp = ORACLE.ledger_total(db, company_id=1, branches=brs, typ="expense", d0=d0, d1=d1)
    got_pur = ORACLE.purchases_total(db, company_id=1, branches=brs, d0=d0, d1=d1)

    assert got_sale - base["sale"] == Decimal("1500.25")
    assert got_tax - base["tax"] == Decimal("120.05")
    assert got_exp - base["expense"] == Decimal("200.02")
    # the rejected purchase (55.55) must be excluded from the delta
    assert got_pur - base["purchases"] == Decimal("400.20")


def test_oracle_excludes_other_company_and_other_period(seeded):
    """Boundary predicates: company and posting period must both bind."""
    db, brs, d0, d1, base = seeded
    c2 = ORACLE.ledger_total(db, company_id=2, branches=brs, typ="sale", d0=d0, d1=d1)
    assert c2 - base["sale_c2"] == Decimal("9999.99"), "company predicate is not binding"

    # widening the window pulls in the prior-period row; the bounded call must not
    wide = ORACLE.ledger_total(db, company_id=1, branches=brs, typ="sale",
                               d0=date(1970, 1, 1), d1=date(2999, 1, 1))
    bounded = ORACLE.ledger_total(db, company_id=1, branches=brs, typ="sale", d0=d0, d1=d1)
    assert wide - base["sale_wide"] == Decimal("9278.02")
    assert bounded - base["sale"] == Decimal("1500.25")
    assert wide - bounded >= Decimal("7777.77"), "period predicate is not binding"


@pytest.mark.parametrize("d,expected", [(-1, False), (0, True), (1, True)])
def test_period_boundaries_are_inclusive(seeded, d, expected):
    """d0-1 excluded; d0 and d1 included."""
    db, brs, d0, d1, base = seeded
    before = ORACLE.ledger_total(db, company_id=1, branches=brs, typ="deposit", d0=d0, d1=d1)
    probe = models.Ledger(branch=brs[0], type="deposit", amount=Decimal("11.11"),
                          entry_date=d0 + timedelta(days=d), company_id=1, memo=_MARK)
    db.add(probe)
    db.commit()
    got = ORACLE.ledger_total(db, company_id=1, branches=brs, typ="deposit", d0=d0, d1=d1)
    assert ((got - before) == Decimal("11.11")) is expected


# --------------------------------------------------------------------------
# C1 — mutation matrix. Production aggregation only; the oracle is untouched.
# --------------------------------------------------------------------------

class _MutateProduction:
    """Patch a helper in the production modules that bind it.

    control.py does `from .core import _sum, _purchases_sum`, so a real bug in
    core would be visible under both names. The oracle module is deliberately
    NOT patched — that is the whole point of the test.
    """

    def __init__(self, name, fn):
        self.name, self.fn, self.saved = name, fn, {}

    def __enter__(self):
        for mod in (CORE, CTRL):
            if hasattr(mod, self.name):
                self.saved[mod] = getattr(mod, self.name)
                setattr(mod, self.name, self.fn)
        assert self.saved, f"{self.name} was not bound anywhere — mutation would be a no-op"
        return self

    def __exit__(self, *exc):
        for mod, old in self.saved.items():
            setattr(mod, self.name, old)
        return False


def _accounting_failures(rep):
    out = []
    for sec in rep["sections"]:
        for c in sec["checks"]:
            if c["status"] in ("critical", "error"):
                out.append((sec["module"], c["check"], c["status"]))
    return out


def test_clean_state_passes_then_each_mutation_is_caught(seeded):
    """Clean pass, then production-only mutation must go red. Both halves matter."""
    db, brs, d0, d1, base = seeded

    clean = CTRL._run_all(db, None)
    fin_crit = [f for f in _accounting_failures(clean)
                if f[0] == "Accounting" and f[2] == "critical"]
    assert not fin_crit, f"clean state already has critical accounting failures: {fin_crit}"

    real_sum = CORE._sum
    real_psum = CORE._purchases_sum

    mutations = {
        "double-count":
            ("_sum", lambda db_, b_, t_, x0, x1, col="amount":
                real_sum(db_, b_, t_, x0, x1, col) * 2),
        "period-bypass":
            ("_sum", lambda db_, b_, t_, x0, x1, col="amount":
                real_sum(db_, b_, t_, date(1970, 1, 1), date(2999, 1, 1), col)),
        "purchases-zeroed":
            ("_purchases_sum", lambda db_, b_, x0, x1: 0.0),
    }

    for label, (target, fn) in mutations.items():
        with _MutateProduction(target, fn):
            rep = CTRL._run_all(db, None)
        failures = _accounting_failures(rep)
        assert failures, f"mutation {label!r} was not detected by any check"
        assert rep["label"] != "Healthy", (
            f"mutation {label!r} left the headline verdict Healthy: {rep['label']}")


def test_double_count_forces_critical_and_critical_label(seeded):
    """The headline case from the original finding."""
    db, brs, d0, d1, base = seeded
    real_sum = CORE._sum
    with _MutateProduction("_sum", lambda db_, b_, t_, x0, x1, col="amount":
                           real_sum(db_, b_, t_, x0, x1, col) * 2):
        rep = CTRL._run_all(db, None)
    assert rep["totals"]["critical"] > 0, "doubled revenue produced no critical finding"
    assert rep["label"] == "Critical", f"expected Critical, got {rep['label']}"


# --------------------------------------------------------------------------
# C3 — verdict may not be diluted
# --------------------------------------------------------------------------

def test_added_passing_checks_cannot_restore_healthy():
    """Verdict invariance: the label must be decided by what failed, not by count."""
    R = CTRL._Report()
    R.add("Accounting", "a financial reconciliation", False, "critical", "wrong")
    first = R.build()["label"]
    assert first == "Critical", first

    for i in range(500):
        R.add("Performance", f"filler {i}", True)
    after = R.build()
    assert after["label"] == "Critical", (
        f"500 passing checks changed the verdict to {after['label']} "
        f"(score {after['score']})")
    assert after["score"] > 95, "this test is only meaningful while the score is diluted high"


def test_unsupported_blocks_healthy_and_is_not_counted_as_passing():
    R = CTRL._Report()
    R.add("Accounting", "something real", True)
    R.unsupported("Accounting", "COGS reconciles to inventory movement",
                  "UNSUPPORTED / NOT TESTED")
    rep = R.build()
    assert rep["label"] == "Incomplete", rep["label"]
    assert rep["totals"]["passed"] == 1, rep["totals"]
    assert rep["totals"]["unsupported"] == 1, rep["totals"]


def test_cogs_is_declared_unsupported_not_passed(seeded):
    """Purchases may never be certified as COGS again."""
    db, brs, d0, d1, base = seeded
    rep = CTRL._run_all(db, None)
    cogs = [c for sec in rep["sections"] for c in sec["checks"]
            if "cost of goods" in c["check"].lower() or "cogs" in c["check"].lower()]
    assert cogs, "the COGS capability must still be declared, not silently dropped"
    for c in cogs:
        assert c["status"] == "unsupported", f"{c['check']} is {c['status']}, expected unsupported"


def test_security_section_survives_naive_and_aware_timestamps(seeded):
    """Regression: the audit-freshness check must not crash on a naive timestamp.

    SQLite returns naive datetimes, PostgreSQL returns aware ones. Subtracting
    across the two raises, and the Security section's broad `except Exception`
    would collapse every security check into a single "Security checks executed"
    critical — a whole section lost to one unhandled type.
    """
    db, brs, d0, d1, base = seeded
    db.query(models.ValidationRun).delete()
    db.commit()
    db.add(models.ValidationRun(user_id="U-owner", score=99, passed=1, warnings=0,
                                errors=0, critical=0, duration_ms=1, severity="ok"))
    db.commit()

    rep = CTRL._run_all(db, None)
    sec = [c for s in rep["sections"] if s["module"] == "Security" for c in s["checks"]]
    crashed = [c for c in sec if c["check"] == "Security checks executed"]
    assert not crashed, f"Security section crashed: {crashed}"
    assert any(c["check"] == "A previous validation run is on record" for c in sec), \
        "the audit-history check did not run"


def test_payroll_run_must_link_to_exactly_one_posting(seeded):
    """A period total can agree while an individual run points at nothing."""
    db, brs, d0, d1, base = seeded
    db.add(models.PayrollRun(company_id=1, branch=brs[0], period_start=d0, period_end=d1,
                             gross=Decimal("300.03"), ledger_id=None,
                             finalized_by=_MARK))
    db.commit()
    defects = ORACLE.payroll_linkage_defects(db, company_id=1, branches=brs, d0=d0, d1=d1)
    assert defects and "no linked posting" in defects[0], defects
