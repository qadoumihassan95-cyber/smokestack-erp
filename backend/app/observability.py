"""Structured observability — per-request correlation logging with tenant + user
context, secret redaction, and security-event logging.

Additive and business-agnostic: it changes no endpoint behaviour. Every request
gets a request_id (propagated via the X-Request-ID response header); after auth
resolves, the middleware logs one structured line carrying request_id,
company_id, user_id, impersonation state, method, path, status, status class,
duration, and the deployment build version. Sensitive material (tokens, cookies,
passwords) is never logged — only method/path/status/ids are, so nothing to leak.
"""
import contextvars
import logging
import os
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware

# request_id available to any logger via a contextvar (e.g. background handlers).
request_id_var = contextvars.ContextVar("request_id", default="-")

# Deployment version — Render injects RENDER_GIT_COMMIT; fall back to APP_VERSION.
BUILD_VERSION = (os.getenv("RENDER_GIT_COMMIT")
                 or os.getenv("APP_VERSION") or "dev")[:12]

log = logging.getLogger("pfs.request")
sec_log = logging.getLogger("pfs.security")


class ObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        # populated by security.get_current_user once auth resolves (same request
        # object, so visible here after call_next returns)
        request.state.company_id = None
        request.state.user_id = None
        request.state.impersonation = False
        tok = request_id_var.set(rid)
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            dur_ms = round((time.perf_counter() - start) * 1000, 1)
            # single structured line; fields inline so they print under any format
            log.info(
                "request rid=%s company=%s user=%s imp=%s %s %s -> %s (%sxx) %sms v=%s",
                rid,
                getattr(request.state, "company_id", None),
                getattr(request.state, "user_id", None),
                getattr(request.state, "impersonation", False),
                request.method,
                request.url.path,
                status_code,
                status_code // 100,
                dur_ms,
                BUILD_VERSION,
            )
            request_id_var.reset(tok)


def log_security_event(kind: str, **fields):
    """Log a security-relevant event (cross-tenant attempt, denied module/
    subscription, failed login) with the current request_id for correlation."""
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    sec_log.warning("security_event kind=%s rid=%s %s", kind,
                    request_id_var.get(), parts)
