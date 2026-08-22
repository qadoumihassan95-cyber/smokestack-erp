"""Control Center login throttle (SEC-11) — the Control Center's own.

WHY THIS IS NOT `app.ratelimit`. The Control Center is decoupled on purpose so it can
be extracted to its own service: `tests/test_pfs_decoupling.py` asserts it shares only
`database` and `models` with the ERP. Importing the ERP's rate limiter would have made
the sub-app un-extractable, which is a real architectural cost for a small saving. The
first draft of this fix did exactly that and the decoupling test caught it.

The storage IS shared — `models.RateHit` is a mapped model, which is inside the allowed
boundary — so this is a different call site over the same table, not a second store.

**Deliberate difference from the ERP limiter: this never reads `X-Forwarded-For`.**
The ERP needs the real client address because a shared bucket there would throttle a
whole customer base together (SEC HIGH-02, `TRUSTED_PROXY_HOPS`). Here the population
is a handful of platform administrators who sign in rarely, so behind a proxy every
request collapsing to one bucket is acceptable — and it fails in the safe direction:
the bucket can only ever be MORE crowded than reality, never less. A header the caller
writes cannot buy this endpoint a fresh bucket at any hop count, because it is not read.

The per-username limit is the one that actually bounds a targeted attack, and it is not
address-derived at all.
"""
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from .. import models

KIND = "pfs_login"
# Tighter than the tenant login's 10/300s. This credential reaches every tenant, and
# legitimate platform sign-ins are rare.
PER_USER_LIMIT = 5
PER_ADDR_LIMIT = 15
WINDOW_SEC = 300


def client_addr(request) -> str:
    """The socket peer, and only ever the socket peer. See the module docstring."""
    if request is None:
        return "unknown"
    return getattr(getattr(request, "client", None), "host", None) or "unknown"


def _count(db, key, cutoff):
    return (db.query(models.RateHit)
            .filter(models.RateHit.scope_key == key, models.RateHit.ts >= cutoff)
            .count())


def _check(db, key, limit):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=WINDOW_SEC)
    db.query(models.RateHit).filter(
        models.RateHit.scope_key == key, models.RateHit.ts < cutoff
    ).delete(synchronize_session=False)
    if _count(db, key, cutoff) >= limit:
        raise HTTPException(429, "Too many attempts. Please wait and try again.",
                            {"Retry-After": str(WINDOW_SEC)})


def guard(db, request, username) -> None:
    """Throttle BEFORE verifying the secret, on both the account and the address."""
    _check(db, f"{KIND}:addr:{client_addr(request)}", PER_ADDR_LIMIT)
    if username:
        _check(db, f"{KIND}:user:{str(username).lower()}", PER_USER_LIMIT)


def note_failure(db, request, username) -> None:
    """Register one failed attempt. Best-effort: never turn a 401 into a 500."""
    try:
        db.add(models.RateHit(scope_key=f"{KIND}:addr:{client_addr(request)}",
                              ts=datetime.now(timezone.utc)))
        if username:
            db.add(models.RateHit(scope_key=f"{KIND}:user:{str(username).lower()}",
                                  ts=datetime.now(timezone.utc)))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
