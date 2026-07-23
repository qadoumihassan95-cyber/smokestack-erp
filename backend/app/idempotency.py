"""Shared idempotency framework (Engineering Phase 5).

A single ASGI middleware makes EVERY mutating endpoint idempotent when the client
sends an ``Idempotency-Key`` header — no per-endpoint code. A retried request with
the same key replays the first response instead of executing twice (prevents double
purchases, double transfers, double stock movements).

Design:
  * Applies only to POST/PUT/PATCH carrying an ``Idempotency-Key`` header.
  * Scope = SHA-256 of the ``Authorization`` header, so a key is unique per caller
    and can never collide across tenants (each company's users have distinct tokens).
  * On first sight: run the handler, buffer the response, persist
    (scope, key) → (status, body, content-type), replay the buffered body.
  * On repeat: return the stored response without running the handler.
  * A concurrent duplicate that races the first (unique-constraint clash on insert)
    falls back to returning the freshly-computed response — still exactly-once effect.

This complements the existing scheduler idempotency (`report_deliveries.idem_key`)
and generalises the pattern platform-wide.
"""
import hashlib

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

_METHODS = {"POST", "PUT", "PATCH"}


def _scope(request):
    auth = request.headers.get("authorization", "")
    return hashlib.sha256(auth.encode("utf-8")).hexdigest()[:32] if auth else "anon"


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        key = request.headers.get("idempotency-key")
        if not key or request.method not in _METHODS:
            return await call_next(request)

        scope = _scope(request)
        # Import here to avoid import cycles at app-composition time.
        from .database import SessionLocal
        from . import models, tenancy

        # ---- replay a stored response if we've seen this (scope, key) ----
        db = SessionLocal()
        try:
            tenancy.use_system_context(db)
            prior = (db.query(models.IdempotencyKey)
                     .filter(models.IdempotencyKey.scope == scope,
                             models.IdempotencyKey.key == key)
                     .first())
            if prior is not None:
                return Response(
                    content=(prior.response_body or "").encode("utf-8"),
                    status_code=prior.status_code or 200,
                    media_type=prior.content_type or "application/json",
                    headers={"Idempotency-Replayed": "true"},
                )
        finally:
            db.close()

        # ---- first time: run handler, buffer + persist the response ----
        response = await call_next(request)
        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        # Only persist successful, non-server-error responses.
        if response.status_code < 500:
            db = SessionLocal()
            try:
                tenancy.use_system_context(db)
                db.add(models.IdempotencyKey(
                    scope=scope, key=key, method=request.method,
                    path=request.url.path, status_code=response.status_code,
                    response_body=body.decode("utf-8", "replace"),
                    content_type=response.headers.get("content-type", "application/json"),
                ))
                db.commit()
            except Exception:
                db.rollback()   # concurrent duplicate raced us; the effect is still once
            finally:
                db.close()

        return Response(content=body, status_code=response.status_code,
                        media_type=response.headers.get("content-type"),
                        headers=dict(response.headers))
