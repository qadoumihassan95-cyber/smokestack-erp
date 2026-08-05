#!/usr/bin/env python3
"""Launch cleanup — remove demo/test operational data, leave a clean, launch-ready DB.

SmokeStack Task 3. Pre-launch the client has entered nothing, so every operational
row is seed/demo/test data. This tool wipes the OPERATIONAL tables while preserving
schema, migration history, branches (keys + display names + settings), the verified
owner/admin accounts, roles/RBAC, platform/config, and Telegram/Render config.

HARD SAFETY (all enforced here):
  * DRY-RUN by default — prints a per-table inventory of what would be deleted vs
    preserved. Nothing changes without --apply.
  * Requires --verified-owner: a real, active, NON-default owner that must survive.
    Fails closed (verify_replacement) if that account is missing/invalid.
  * Refuses to run unless SEED_ON_START is off (so wiped demo data can't respawn).
  * Requires --backup-confirmed before --apply (operator asserts a recoverable
    backup was taken and verified immediately beforehand).
  * Runs the wipe in ONE transaction; resets safe sequences; never touches a table
    that isn't on the explicit WIPE list (unknown tables are preserved fail-safe).
  * Reports COUNTS ONLY — never row contents, secrets, or credentials.

This tool is deliberately NOT wired to run anywhere automatically. Run it manually,
dry-run first, and only against production after explicit approval + a verified
backup + confirmation that a legitimate owner can log in.

Usage (from backend/):
    # dry-run inventory (safe):
    python -m scripts.cleanup_launch_data --verified-owner U-realowner
    # execute (after backup + approval):
    python -m scripts.cleanup_launch_data --verified-owner U-realowner \
        --backup-confirmed --apply
"""
from __future__ import annotations

import argparse
import sys

# Canonical seed/default accounts (see app/seed.py USERS) — never accepted as the
# "verified real owner", and identified separately from real accounts.
DEFAULT_ACCOUNT_IDS = ["U-owner", "U-admin", "U-bm", "U-inv", "U-acct", "U-cash", "U-emp"]


def verify_replacement(users, verified_owner_id):
    """Fail-closed: the nominated survivor must be present, role 'owner', active, and
    NOT a default seeded id. Returns the user or raises ValueError."""
    if not verified_owner_id:
        raise ValueError("A --verified-owner must be supplied.")
    if verified_owner_id in DEFAULT_ACCOUNT_IDS:
        raise ValueError(f"'{verified_owner_id}' is a default seeded account; nominate a real owner.")
    u = users.get(verified_owner_id)
    if u is None:
        raise ValueError(f"Verified owner '{verified_owner_id}' not found.")
    if u.role != "owner":
        raise ValueError(f"Verified owner '{verified_owner_id}' has role '{u.role}', not 'owner'.")
    if (u.status or "active") != "active":
        raise ValueError(f"Verified owner '{verified_owner_id}' is not active.")
    return u


# Operational demo/test data — wiped whole (children before parents for FK safety).
WIPE_ORDER = [
    # Team Chat (+ durable attachments)
    "chat_attachments", "chat_reactions", "chat_tasks", "chat_members",
    "chat_announcements", "chat_presence", "chat_messages", "chat_rooms",
    # Telegram links / evidence / deliveries / codes
    "attendance_evidence", "telegram_delivery_log", "link_codes", "telegram_links",
    # Attendance & schedules & reminders
    "clock_events", "attendance", "schedule_exceptions", "employee_schedules",
    "schedule_templates", "reminder_deliveries", "reminder_settings",
    # Reports delivery test records
    "report_deliveries", "report_recipients",
    # Inventory & purchasing & sales/ledger
    "movements", "stock", "transfers", "purchases", "products", "approvals", "ledger",
    # Partners, licences, employees (demo)
    "licenses", "customers", "suppliers", "employees",
    # Alerts / notifications / test artifacts / testing audit
    "no_activity_incidents", "idempotency_keys", "validation_runs", "audit_log",
]

# Structure, identity, RBAC, platform + config — NEVER wiped.
PRESERVE = {
    "branches", "users", "user_branches",
    "companies", "company_modules", "company_settings", "subscriptions",
    "modules", "applications", "feature_flags", "policy_overrides",
    "document_counters", "platform_users", "platform_audit", "alembic_version",
}

# Sequences safe to reset after a full wipe (surrogate PKs; no external references kept).
RESET_SEQUENCES_FOR = set(WIPE_ORDER)


def _all_tables(db):
    from sqlalchemy import inspect
    return set(inspect(db.get_bind()).get_table_names())


def _count(db, table):
    from sqlalchemy import text
    try:
        # table is from the DB inspector / fixed allowlist, never user input.
        return db.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar() or 0  # nosec B608
    except Exception:  # table absent in this DB
        return None


def classify(tables):
    """Split the live tables into wipe / preserve / unknown(→preserve, fail-safe)."""
    wipe = [t for t in WIPE_ORDER if t in tables]
    preserve = sorted(t for t in tables if t in PRESERVE)
    unknown = sorted(t for t in tables if t not in set(WIPE_ORDER) and t not in PRESERVE)
    return wipe, preserve, unknown


def inventory(db):
    tables = _all_tables(db)
    wipe, preserve, unknown = classify(tables)
    return {
        "wipe": [(t, _count(db, t)) for t in wipe],
        "preserve": [(t, _count(db, t)) for t in preserve],
        "unknown_preserved": [(t, _count(db, t)) for t in unknown],
    }


def verify_owner_can_login(db, verified_owner_id):
    """Fail-closed: the nominated owner must be real, non-default, active, and
    web-login-capable, so the cleanup can never lock out the last legitimate owner."""
    from app import models
    users = {u.id: u for u in db.query(models.User).all()}
    owner = verify_replacement(users, verified_owner_id)  # role owner, active, non-default
    if getattr(owner, "can_login", True) is False:
        raise ValueError(f"Verified owner '{verified_owner_id}' cannot sign in to the web app.")
    # Must remain at least one active owner after cleanup (users table is preserved,
    # so this is guaranteed, but we assert it explicitly).
    active_owners = [u.id for u in users.values()
                     if u.role == "owner" and (u.status or "active") == "active"]
    if verified_owner_id not in active_owners:
        raise ValueError("Verified owner is not an active owner.")
    return owner


def run(db, verified_owner_id, apply=False, backup_confirmed=False):
    """Dry-run (default) or execute the cleanup. Returns a counts-only report dict."""
    from sqlalchemy import text
    from app.config import settings

    # Guard: seeding must be off so wiped demo data can't be recreated on next boot.
    if settings.seed_on_start:
        raise ValueError("SEED_ON_START is ON — refusing to clean data that would be re-seeded on boot.")

    owner = verify_owner_can_login(db, verified_owner_id)
    inv = inventory(db)

    if not apply:
        return {"applied": False, "verified_owner": owner.id,
                "seed_on_start": settings.seed_on_start, **inv}

    if not backup_confirmed:
        raise ValueError("--backup-confirmed is required before --apply (take + verify a DB backup first).")

    deleted = {}
    bind = db.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    # One controlled transaction.
    for t in [x for x in WIPE_ORDER if x in _all_tables(db)]:
        before = _count(db, t)
        # t is from the fixed WIPE_ORDER allowlist intersected with live tables.
        db.execute(text(f'DELETE FROM "{t}"'))  # nosec B608
        deleted[t] = before
        if is_pg and t in RESET_SEQUENCES_FOR:
            # Reset identity sequence if one exists; never touches keys/relationships.
            db.execute(text(
                "SELECT setval(pg_get_serial_sequence(:t, 'id'), 1, false) "
                "WHERE pg_get_serial_sequence(:t, 'id') IS NOT NULL"
            ), {"t": t})
    db.commit()

    post = inventory(db)
    return {"applied": True, "verified_owner": owner.id, "deleted": deleted,
            "post_counts": post["wipe"], "preserved": post["preserve"],
            "unknown_preserved": post["unknown_preserved"]}


# --------------------------------------------------------------------------- CLI
def _print_inventory(inv):
    def line(rows):
        for t, c in rows:
            print(f"    {t:<24} {'(absent)' if c is None else c}")
    print("\n=== WILL DELETE (demo/test operational data) ===");  line(inv["wipe"])
    print("\n=== PRESERVE (schema/identity/RBAC/config) ===");    line(inv["preserve"])
    if inv["unknown_preserved"]:
        print("\n=== UNCLASSIFIED → PRESERVED (fail-safe; review) ==="); line(inv["unknown_preserved"])


def _target(db):
    url = str(db.get_bind().url)
    # host/db only — never credentials
    try:
        u = db.get_bind().url
        return f"{u.drivername} host={u.host or 'local'} db={u.database}"
    except Exception:
        return url.split("@")[-1]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Wipe demo/test operational data for client launch.")
    ap.add_argument("--verified-owner", required=True, help="Real, active, non-default OWNER that must survive.")
    ap.add_argument("--database-url", default=None, help="Override DATABASE_URL (else env; empty => SQLite).")
    ap.add_argument("--backup-confirmed", action="store_true", help="Assert a verified backup was just taken.")
    ap.add_argument("--apply", action="store_true", help="Execute. Omit for a safe dry run.")
    args = ap.parse_args(argv)

    if args.database_url:
        import os
        os.environ["DATABASE_URL"] = args.database_url

    from app.database import SessionLocal
    try:
        from app import tenancy
    except Exception:  # noqa: BLE001
        tenancy = None
    db = SessionLocal()
    if tenancy is not None:
        try:
            tenancy.use_system_context(db)
        except Exception:  # noqa: BLE001
            pass
    try:
        print(f"Target database: {_target(db)}")
        try:
            result = run(db, args.verified_owner, apply=args.apply, backup_confirmed=args.backup_confirmed)
        except ValueError as e:
            print(f"\nABORTED (fail-closed guardrail): {e}", file=sys.stderr)
            return 2
        if not result["applied"]:
            print(f"Verified owner (must survive): {result['verified_owner']}   SEED_ON_START={result['seed_on_start']}")
            _print_inventory(result)
            print("\nDRY RUN — nothing was changed. Re-run with --backup-confirmed --apply after a verified backup.")
        else:
            print(f"\nCLEANUP APPLIED. Verified owner preserved: {result['verified_owner']}")
            print("Deleted (counts):")
            for t, c in result["deleted"].items():
                print(f"    {t:<24} {c}")
            print("Post-cleanup WIPE tables now:")
            for t, c in result["post_counts"]:
                print(f"    {t:<24} {c}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
