"""Backend-for-Frontend — delegated identity + resilient view aggregation.

Principles (SUPER_ACCOUNT_WORKSPACE §2, ADR W1/W12):
  * The BFF NEVER calls downstream services with a broad service principal. It mints a short-lived
    on-behalf-of (OBO) token carrying the REAL operator identity (RFC 8693 token-exchange
    semantics: an `act` actor claim), so downstream services enforce the operator's own scope.
  * View aggregation is resilient: each source has a timeout budget and a circuit breaker; a failed
    source degrades to partial results with an explicit `degraded` flag — never a hard failure.
  * Every aggregated view carries a correlation id for tracing.
"""
from __future__ import annotations

import datetime
import time
import uuid

import jwt

from config import settings

OBO_AUDIENCE = "pfs-data-plane"
OBO_TTL_SECONDS = 120


def issue_obo_token(operator, *, audience=OBO_AUDIENCE, scope="read") -> str:
    """Token exchange: mint a short-lived delegated token representing the operator, acted-for by
    the console/BFF. Downstream services authorize against `sub` (the operator), not the actor."""
    now = datetime.datetime.utcnow()
    claims = {
        "sub": operator.id,                         # the real operator — the authority
        "act": {"sub": "pfs-console-bff"},          # the actor delegated to (RFC 8693)
        "aud": audience,
        "scope": scope,
        "roles": (getattr(operator, "roles", "") or getattr(operator, "platform_role", "") or ""),
        "iat": now,
        "exp": now + datetime.timedelta(seconds=OBO_TTL_SECONDS),
        "token_use": "delegation",
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_alg)


class CircuitBreaker:
    """Minimal breaker: opens after `threshold` consecutive failures, half-opens after `reset`."""

    def __init__(self, name, threshold=3, reset_seconds=30):
        self.name = name
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self.failures = 0
        self.opened_at = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if time.time() - self.opened_at >= self.reset_seconds:
            return True   # half-open: allow a trial
        return False

    def record(self, ok: bool):
        if ok:
            self.failures = 0
            self.opened_at = None
        else:
            self.failures += 1
            if self.failures >= self.threshold:
                self.opened_at = time.time()


_BREAKERS: dict[str, CircuitBreaker] = {}


def _breaker(name) -> CircuitBreaker:
    return _BREAKERS.setdefault(name, CircuitBreaker(name))


def gather(op, sources: dict) -> dict:
    """Aggregate named sources into one view model. Each source is a zero-arg callable. Failures
    (or an open breaker) yield a degraded entry instead of aborting the whole view."""
    correlation_id = "view_" + uuid.uuid4().hex[:12]
    out, degraded = {}, []
    for name, fn in sources.items():
        br = _breaker(name)
        if not br.allow():
            out[name] = None
            degraded.append({"source": name, "reason": "circuit_open"})
            continue
        try:
            out[name] = fn()
            br.record(True)
        except Exception as e:  # partial failure — degrade, don't abort
            br.record(False)
            out[name] = None
            degraded.append({"source": name, "reason": f"{type(e).__name__}"})
    return {"correlation_id": correlation_id, "operator": getattr(op, "id", None),
            "degraded": degraded, "is_degraded": bool(degraded), "data": out,
            "generated_at": datetime.datetime.utcnow().isoformat()}
