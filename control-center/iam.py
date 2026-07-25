"""Operator IAM & Governance — Mission Control foundation (Milestone 1 skeleton).

The "Super Account" is a Platform Owner Organization, not one omnipotent login. This module is
the authorization Policy Decision Point (PDP): least-privilege roles + ABAC scopes, deny-by-default.

Backward compatibility: the live control center seeds a single operator with the legacy coarse
`platform_role` (owner|operator|internal). `operator_roles()` maps that to the new role set so
existing flows keep working while the fine-grained model is populated over subsequent milestones.

Nothing here bypasses anything: the Platform Owner also passes through the PDP — the owner simply
holds the wildcard capability. Authority is never read from client input, only from the operator
identity carried by the validated token (ADR W2).
"""
from __future__ import annotations

import json

# The ten first-class operator roles (SUPER_ACCOUNT_WORKSPACE §3).
ROLES = [
    "platform_owner", "operator", "support_engineer", "release_manager",
    "compliance_officer", "billing_admin", "security_officer", "privacy_officer",
    "incident_commander", "read_only_auditor",
]

# Role → capabilities. "*" = all; a trailing ".*" grants a namespace. Deny-by-default: a role
# grants only what is listed. Kept intentionally small for the M1 slice; extended per phase.
ROLE_CAPABILITIES = {
    "platform_owner": {"*"},
    "operator": {"feature_flag.set_state"},
    "support_engineer": {"support.*", "remote.diagnostics"},
    "release_manager": {"feature_flag.set_state", "feature_flag.write", "release.*", "deploy.*"},
    "compliance_officer": {"compliance.*", "feature_flag.write"},
    "billing_admin": {"billing.*"},
    "security_officer": {"security.*", "iam.revoke_session"},
    "privacy_officer": {"privacy.*", "dsar.*"},
    "incident_commander": {"incident.*"},
    "read_only_auditor": set(),  # no mutating capability
}

# Legacy coarse role → new roles (keeps the seeded owner fully functional).
_LEGACY_MAP = {"owner": ["platform_owner"], "operator": ["operator"], "internal": ["operator"]}

# Commands that must never be self-approved (separation of duties). M1 seeds a representative set;
# the per-operation god-tier classification (SUPER_ACCOUNT_WORKSPACE §7a) extends this.
SOD_REQUIRED = {
    "feature_flag.set_state:high", "feature_flag.set_state:irreversible",
    "release.sign", "tenant.transfer", "ledger.correct", "customer.restore", "customer.suspend_bulk",
}


class Decision:
    __slots__ = ("allow", "reason")

    def __init__(self, allow: bool, reason: str):
        self.allow = allow
        self.reason = reason

    def __bool__(self):
        return self.allow


def operator_roles(op) -> list[str]:
    """Fine-grained roles for an operator, falling back to the legacy coarse role."""
    raw = (getattr(op, "roles", "") or "").strip()
    if raw:
        return [r.strip() for r in raw.split(",") if r.strip()]
    return list(_LEGACY_MAP.get(getattr(op, "platform_role", None), []))


def operator_scopes(op) -> dict:
    """ABAC scope dict. Empty/absent scope for a legacy owner ⇒ full ('*') scope so the live owner
    is unaffected; every other operator is deny-by-default outside its explicit scope."""
    raw = (getattr(op, "scopes", "") or "").strip()
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            return {}
    if "platform_owner" in operator_roles(op):
        return {"erp": ["*"], "region": ["*"], "env": ["*"], "customer": ["*"]}
    return {}


def _has_capability(caps: set[str], action: str) -> bool:
    if "*" in caps:
        return True
    if action in caps:
        return True
    return any(c.endswith(".*") and action.startswith(c[:-1]) for c in caps)


def capabilities(op) -> set[str]:
    out: set[str] = set()
    for r in operator_roles(op):
        out |= ROLE_CAPABILITIES.get(r, set())
    return out


def _scope_ok(scope: dict, dim: str, value) -> bool:
    """A target dimension is allowed if the operator has no constraint on it, or the value is
    within the allowed set (or '*'). Deny-by-default only bites once a scope is declared."""
    allowed = scope.get(dim)
    if allowed is None:            # operator not constrained on this dimension
        return True
    if "*" in allowed:
        return True
    return value is None or str(value) in [str(a) for a in allowed]


def decide(op, action: str, target: dict | None = None, ctx: dict | None = None) -> Decision:
    """Deny-by-default authorization. `target` may carry erp/region/env/customer for ABAC."""
    if op is None:
        return Decision(False, "no_operator")
    if getattr(op, "status", "active") != "active":
        return Decision(False, "operator_inactive")
    if not _has_capability(capabilities(op), action):
        return Decision(False, "capability_not_granted")
    target = target or {}
    scope = operator_scopes(op)
    for dim, key in (("erp", "erp_product_id"), ("region", "region"),
                     ("env", "environment"), ("customer", "customer_ref")):
        if key in target and not _scope_ok(scope, dim, target.get(key)):
            return Decision(False, f"out_of_scope:{dim}")
    return Decision(True, "granted")


def requires_sod(action: str, blast_radius: str) -> bool:
    """True when the command may not be approved by its own requester."""
    return f"{action}:{blast_radius}" in SOD_REQUIRED or action in SOD_REQUIRED


# God-tier actions that require an active break-glass elevation grant (JIT, time-boxed, recorded).
BREAK_GLASS_REQUIRED = {
    "tenant.transfer", "ledger.correct", "platform.kill_switch", "customer.restore",
    "system.recovery", "database.migrate", "maintenance.global_read_only",
}


def requires_break_glass(action: str, blast_radius: str = "") -> bool:
    return action in BREAK_GLASS_REQUIRED
