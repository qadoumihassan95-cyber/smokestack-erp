# Migration Review — M1–M3 (0004, 0005, 0006)

All foundation migrations were reviewed against the required safety criteria and re-verified.

## Criteria & result

| Criterion | 0004_mc_foundation | 0005_mc_m2 | 0006_bulk_ops |
|---|---|---|---|
| **Expand/Contract** (expand only; no contract in same release) | ✓ add columns + tables only | ✓ tables + 1 additive col | ✓ tables + 2 additive cols |
| **No destructive change** (no drop/rename/type-change of live data) | ✓ | ✓ | ✓ |
| **Additive only** | ✓ | ✓ | ✓ |
| **Inspector-guarded** (safe on fresh where create_all already made objects, and on existing) | ✓ `_has_table`/`_has_col` guards | ✓ | ✓ |
| **Idempotent** (re-running upgrade is a no-op on existing objects) | ✓ | ✓ | ✓ |
| **Reversible** (downgrade drops exactly what upgrade added) | ✓ | ✓ | ✓ |
| **Rollback tested** (`up → down → up`) | ✓ verified | ✓ verified | ✓ verified |
| **Full-chain tested** (`upgrade head → downgrade base → upgrade head`) | ✓ 26 tables restored, no error | | |
| **Live-data safety** (Company #1 / SmokeStack unaffected) | ✓ additive; defaults preserve behavior | ✓ | ✓ |

## Notes
- New columns carry safe server defaults (`roles=''`, `version=1`, `mfa_enabled=false`,
  `region='us'`, audit chain nullable) so existing rows and the live business behave identically.
- SQLite column drops in downgrade use `batch_alter_table`; audit-provenance columns on
  `platform_audit_log` were left **unindexed** specifically so the batch recreate remains reversible.
- `0001_init` remains the create_all baseline; 0002+ are additive with guards (established pattern).
- No data transformation or backfill is performed by any foundation migration (pure schema-add).

**Verdict: all three migrations meet the expand/contract, additive, guarded, idempotent, reversible
bar and are safe to apply to the live Development database with no downtime.**
