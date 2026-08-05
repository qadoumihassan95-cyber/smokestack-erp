#!/usr/bin/env python3
"""One-time, auditable remediation of the default seeded (`U-*`) accounts.

Phase-1 security remediation (audit finding C1). The SmokeStack seed creates one
account per role with a shared default password. On any environment that was
seeded, those default-credential accounts must be neutralised. This tool does
that **safely and reversibly**, with hard guardrails:

  * DRY-RUN by default. Nothing changes without an explicit ``--apply``.
  * NEVER disables the last active owner, and never disables the verified
    replacement owner/admin you nominate.
  * REQUIRES a verified, non-default replacement owner (``--verified-owner``)
    to exist and be active before it will disable any privileged default
    account. Fails closed if that account is missing/invalid.
  * Records every account-security action in the audit log (source=SECURITY).
  * NEVER prints, stores, commits, or transmits any password or hash. For
    ``--mode force-reset`` a throwaway random secret is generated, used to
    invalidate the known default password, and immediately discarded.

It is deliberately NOT wired to run automatically anywhere. Run it manually,
first as a dry run, and only against production after explicit owner approval
and a fresh database backup.

Usage (from ``backend/``):

    # 1) Inspect the default-account inventory + planned actions (safe):
    python -m scripts.remediate_default_accounts --verified-owner U-realowner

    # 2) Apply, disabling the default accounts (after backup + approval):
    python -m scripts.remediate_default_accounts \
        --verified-owner U-realowner --mode disable --apply

``--database-url`` (or the DATABASE_URL env var) selects the target DB; empty =>
local SQLite. This module also exposes pure helper functions so the guardrails
can be unit-tested without a live database mutation.
"""
from __future__ import annotations

import argparse
import secrets
import sys
from types import SimpleNamespace

# The canonical set of accounts the seed creates (see app/seed.py USERS).
DEFAULT_ACCOUNT_IDS = ["U-owner", "U-admin", "U-bm", "U-inv", "U-acct", "U-cash", "U-emp"]

ACTIVE = "active"
DISABLED = "disabled"


# --------------------------------------------------------------------------- #
# Pure, testable helpers (no I/O, no mutation)
# --------------------------------------------------------------------------- #
def is_active_owner(u) -> bool:
    return u is not None and u.role == "owner" and (u.status or ACTIVE) == ACTIVE


def verify_replacement(users, verified_owner_id, default_ids=DEFAULT_ACCOUNT_IDS):
    """Return the verified replacement owner or raise ValueError (fail-closed).

    A valid replacement is an account that is: present, role 'owner', active, and
    NOT one of the default seeded ids. This guarantees a legitimate owner exists
    before any default privileged account is touched.
    """
    if not verified_owner_id:
        raise ValueError("A --verified-owner must be supplied before disabling default accounts.")
    if verified_owner_id in default_ids:
        raise ValueError(
            f"Verified owner '{verified_owner_id}' is itself a default seeded account; "
            "nominate a real, non-default owner."
        )
    u = users.get(verified_owner_id)
    if u is None:
        raise ValueError(f"Verified owner '{verified_owner_id}' was not found in the target database.")
    if u.role != "owner":
        raise ValueError(f"Verified owner '{verified_owner_id}' has role '{u.role}', not 'owner'.")
    if (u.status or ACTIVE) != ACTIVE:
        raise ValueError(f"Verified owner '{verified_owner_id}' is not active.")
    return u


def plan_targets(users, requested_ids, verified_owner_id, verified_admin_id=None):
    """Decide which requested accounts will actually be acted on.

    Excludes: accounts that don't exist, the verified owner/admin, and accounts
    already disabled (idempotent). Returns (to_act, skipped) where each entry is
    (id, role, current_status, reason_if_skipped).
    """
    protect = {verified_owner_id, verified_admin_id} - {None}
    to_act, skipped = [], []
    for aid in requested_ids:
        u = users.get(aid)
        if u is None:
            skipped.append((aid, None, None, "not present"))
            continue
        status = (u.status or ACTIVE)
        if aid in protect:
            skipped.append((aid, u.role, status, "protected (verified replacement)"))
            continue
        if status != ACTIVE:
            skipped.append((aid, u.role, status, "already disabled (idempotent)"))
            continue
        to_act.append((aid, u.role, status, None))
    return to_act, skipped


def assert_owner_survives(users, ids_being_disabled, verified_owner_id):
    """Refuse if the operation would remove the last active owner.

    The set of active owners remaining AFTER the disable must be non-empty and
    must include the verified replacement owner.
    """
    disabling = set(ids_being_disabled)
    remaining_owners = [
        uid for uid, u in users.items()
        if is_active_owner(u) and uid not in disabling
    ]
    if not remaining_owners:
        raise ValueError("Refusing to proceed: this would disable the last active owner account.")
    if verified_owner_id not in remaining_owners:
        raise ValueError(
            "Refusing to proceed: the verified owner would not remain an active owner afterwards."
        )
    return remaining_owners


def effective_permissions(role):
    """Effective permission list for a role, for the inventory report (no secrets)."""
    from app import permissions as P
    return list(P.PERMS.get(role, []))


# --------------------------------------------------------------------------- #
# DB-facing orchestration
# --------------------------------------------------------------------------- #
def _load_users(db):
    from app import models
    return {u.id: u for u in db.query(models.User).all()}


def build_inventory(db, default_ids=DEFAULT_ACCOUNT_IDS):
    """Inventory of the default accounts present, by id/role/status + effective perms.
    Contains NO credentials or hashes."""
    users = _load_users(db)
    rows = []
    for aid in default_ids:
        u = users.get(aid)
        if u is None:
            rows.append({"id": aid, "present": False})
            continue
        rows.append({
            "id": aid, "present": True, "role": u.role,
            "status": (u.status or ACTIVE), "can_login": bool(getattr(u, "can_login", True)),
            "perm_count": len(effective_permissions(u.role)),
            "permissions": effective_permissions(u.role),
        })
    return rows


def remediate(db, verified_owner_id, mode="disable", requested_ids=None,
              verified_admin_id=None, apply=False, actor_id=None):
    """Execute (or dry-run) the remediation. Returns a structured result dict.

    Never prints/stores/returns any password or hash. Writes one audit-log row
    per applied account action (source=SECURITY).
    """
    from app import models, security as S
    requested_ids = list(requested_ids or DEFAULT_ACCOUNT_IDS)
    users = _load_users(db)

    # Guardrail 1: a legitimate, non-default, active owner must exist first.
    verified = verify_replacement(users, verified_owner_id)

    # Decide targets (excludes protected + already-disabled + missing).
    to_act, skipped = plan_targets(users, requested_ids, verified_owner_id, verified_admin_id)
    acting_ids = [aid for (aid, *_rest) in to_act]

    # Guardrail 2: never remove the last active owner.
    remaining_owners = assert_owner_survives(users, acting_ids, verified_owner_id)

    actor = SimpleNamespace(id=(actor_id or verified.id))
    actions = []
    for aid, role, status, _ in to_act:
        u = users[aid]
        if mode == "disable":
            action = "security.disable_account"
            if apply:
                u.status = DISABLED
                if hasattr(u, "can_login"):
                    u.can_login = False
        elif mode == "force-reset":
            action = "security.force_password_reset"
            if apply:
                # Invalidate the known default password with a throwaway secret
                # that is never printed, stored, or returned. An admin then issues
                # a fresh temporary password through the normal out-of-band flow.
                throwaway = secrets.token_urlsafe(48)[:64]
                u.password_hash = S.hash_pw(throwaway)
                del throwaway
                if hasattr(u, "must_change_password"):
                    u.must_change_password = True
        else:
            raise ValueError(f"Unknown mode '{mode}' (use 'disable' or 'force-reset').")
        actions.append({"id": aid, "role": role, "prev_status": status, "action": action})
        if apply:
            # One audit row per action (source=SECURITY). Detail carries role +
            # mode + authorizing owner only — no credential material.
            S.audit(db, actor, action, "user", ref=aid,
                    detail=f"role={role}; mode={mode}; authorized_by={verified.id}",
                    source="SECURITY")

    return {
        "applied": apply,
        "mode": mode,
        "verified_owner": verified.id,
        "remaining_active_owners": remaining_owners,
        "actions": actions,
        "skipped": [{"id": s[0], "role": s[1], "status": s[2], "reason": s[3]} for s in skipped],
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_inventory(rows):
    print("\n=== Default seeded account inventory (id / role / status — NO credentials) ===")
    for r in rows:
        if not r.get("present"):
            print(f"  {r['id']:<10} : not present")
            continue
        print(f"  {r['id']:<10} : role={r['role']:<16} status={r['status']:<9} "
              f"can_login={r['can_login']!s:<5} effective_perms={r['perm_count']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Safely disable/force-reset default seeded accounts.")
    ap.add_argument("--verified-owner", required=True,
                    help="ID of the real, non-default, active OWNER that must survive.")
    ap.add_argument("--verified-admin", default=None,
                    help="Optional ID of a real admin to protect from being disabled.")
    ap.add_argument("--mode", choices=["disable", "force-reset"], default="disable")
    ap.add_argument("--accounts", default=",".join(DEFAULT_ACCOUNT_IDS),
                    help="Comma-separated account ids to remediate (default: all U-* seeds).")
    ap.add_argument("--database-url", default=None,
                    help="Override DATABASE_URL (else env; empty => local SQLite).")
    ap.add_argument("--apply", action="store_true",
                    help="Actually perform the change. Omit for a safe dry run.")
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
        # Cross-tenant maintenance: explicit, audited system context.
        try:
            tenancy.use_system_context(db)
        except Exception:  # noqa: BLE001
            pass
    try:
        rows = build_inventory(db)
        _print_inventory(rows)
        requested = [a.strip() for a in args.accounts.split(",") if a.strip()]
        try:
            result = remediate(
                db, verified_owner_id=args.verified_owner, mode=args.mode,
                requested_ids=requested, verified_admin_id=args.verified_admin,
                apply=args.apply,
            )
        except ValueError as e:
            print(f"\nABORTED (fail-closed guardrail): {e}", file=sys.stderr)
            return 2

        mode_label = "APPLIED" if result["applied"] else "DRY-RUN (no changes written)"
        print(f"\n=== Remediation plan [{mode_label}] mode={result['mode']} ===")
        print(f"  verified owner (protected, must survive): {result['verified_owner']}")
        print(f"  active owners remaining after operation : {result['remaining_active_owners']}")
        if result["actions"]:
            for a in result["actions"]:
                print(f"    -> {a['action']:<32} {a['id']:<10} (role={a['role']}, was {a['prev_status']})")
        else:
            print("    (no accounts require action)")
        if result["skipped"]:
            print("  skipped:")
            for s in result["skipped"]:
                print(f"    -- {s['id']:<10} : {s['reason']}")
        if not result["applied"]:
            print("\nThis was a DRY RUN. Re-run with --apply (after a DB backup + owner approval) to commit.")
        else:
            print("\nDone. Every action above was written to the audit log (source=SECURITY).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
