# Database migrations (Alembic)

Schema changes are versioned with Alembic — **not** a destructive reset.
`create_all` still runs on boot for local/dev convenience, but production schema
evolution goes through Alembic.

## Commands
```bash
# apply all pending migrations (safe, idempotent) — run this on every deploy
alembic upgrade head

# see the current DB revision
alembic current

# see history
alembic history

# after changing models, generate a new migration, then review it before commit
alembic revision --autogenerate -m "describe change"

# roll back one revision (only when a migration is reversible & you've backed up)
alembic downgrade -1
```

## On Render
`render.yaml` runs `alembic upgrade head` as the **preDeployCommand**, so each
deploy migrates the database before the new code serves traffic. Never runs a
destructive reset.

## Backups (do before any risky migration)
```bash
# dump
pg_dump "$DATABASE_URL" -Fc -f backup_$(date +%F).dump
# restore
pg_restore --clean --if-exists -d "$DATABASE_URL" backup_2025-01-01.dump
```
Render also offers automatic daily backups + point-in-time recovery on paid
Postgres plans — enable them in the database's dashboard.

## Notes
- The initial migration (`migrations/versions/*_initial_schema.py`) creates all
  18 tables. Verified with `alembic upgrade head` on a fresh database.
- Downgrades are generated for reversible operations; review before relying on
  them in production, and always back up first.
- Set `SEED_ON_START=false` in production after the first boot so seed data
  isn't re-checked on every start.
