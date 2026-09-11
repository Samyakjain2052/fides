"""
Application factory.

Middleware order matters and is deliberate:
  request id  →  so every log line and error body is correlatable
  CORS        →  before anything that can reject, so failures still get headers
  routes
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.public import (
    public_banner_router,
    public_grievance_router,
    public_rights_router,
    public_v1_router,
)
from app.api.v1 import api_router
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging, request_id_ctx, tenant_id_ctx
from app.db.session import dispose_engine, get_engine

settings = get_settings()
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("DEBUG" if settings.debug else "INFO")
    logger.info("starting %s (env=%s)", settings.project_name, settings.env)
    yield
    await dispose_engine()


app = FastAPI(
    title=settings.project_name,
    version="0.1.0",
    description=(
        "Multi-tenant DPDP Act consent-management API.\n\n"
        "**Tenant isolation** is enforced by PostgreSQL row-level security, not by "
        "application filtering alone. **Permissions** are checked server-side on every "
        "route. **The audit trail** is an HMAC hash chain and is append-only at the "
        "database level."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json",
)

register_error_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # Required for the refresh cookie. Note this is why cors_origins must never
    # be "*" — the config validator rejects that combination in prod.
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-Id",
                   "Idempotency-Key"],
    expose_headers=["X-Request-Id"],
)


# Paths a customer's own website is expected to call from a browser, from an
# origin we cannot know in advance.
#
# The main CORS policy above is an allowlist WITH credentials, and it has to
# stay that way — the refresh cookie depends on it, which is why the config
# validator refuses `*` there. These two paths are the opposite case: they are
# unauthenticated statutory intake, they must work from any customer's domain,
# and they must NOT carry credentials.
#
# Wildcard origin and `allow_credentials` off go together and are not
# separable: a browser refuses `*` alongside credentials, and that refusal is
# the mechanism that makes these endpoints CSRF-safe by construction. Nothing
# reachable here can act on a signed-in session, because no session travels.
_PUBLIC_CORS_PREFIXES = ("/public/v1/rights", "/public/v1/grievance")

_PUBLIC_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "600",
    # No Access-Control-Allow-Credentials. See above.
    "Vary": "Origin",
}


@app.middleware("http")
async def public_intake_cors(request: Request, call_next):
    """Wildcard CORS, for the two unauthenticated statutory paths only.

    Registered after the main CORS middleware, which in Starlette means it runs
    OUTSIDE it — so a preflight for one of these paths is answered here rather
    than being rejected by the allowlist it is not on.
    """
    path = request.url.path
    if not path.startswith(_PUBLIC_CORS_PREFIXES):
        return await call_next(request)

    if request.method == "OPTIONS":
        # Answered here rather than passed down: the allowlist middleware would
        # reject a preflight from an origin it does not know, which is every
        # customer's website.
        return Response(status_code=204, headers=_PUBLIC_CORS_HEADERS)

    response = await call_next(request)
    for header, value in _PUBLIC_CORS_HEADERS.items():
        response.headers[header] = value
    return response


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a request id, time the request, log one structured line.

    The id is echoed in the response header and in every error body, so a customer
    reporting a failure gives us the one string that finds it in the logs.
    """
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    request_id_ctx.set(rid)
    tenant_id_ctx.set(None)

    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # The error handler produces the body; this line records the timing.
        logger.exception(
            "request failed",
            extra={"context": {"method": request.method, "path": request.url.path}},
        )
        raise
    duration_ms = round((time.perf_counter() - started) * 1000, 2)

    response.headers["X-Request-Id"] = rid
    # Cheap wins, independent of the reverse proxy's own headers.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"

    logger.info(
        "request",
        extra={
            "context": {
                "method": request.method,
                # Path only — a query string can carry an email address.
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            }
        },
    )
    return response


@app.get("/health", tags=["meta"], summary="Liveness and readiness")
async def health() -> dict:
    """Reports the database too: a process that is up but cannot reach Postgres is
    not ready to serve, and an orchestrator needs to know the difference."""
    db_ok = True
    detail = "ok"
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - report, never raise, from health
        db_ok = False
        detail = f"unreachable: {type(exc).__name__}"

    return {
        "status": "ok" if db_ok else "degraded",
        "env": settings.env,
        "version": app.version,
        "database": detail,
    }


app.include_router(api_router, prefix=settings.api_prefix)

# The public API is mounted at its own root, NOT under settings.api_prefix.
# Customers deploy code against these paths; they must not move when the admin
# API's version prefix changes. That is the whole reason the two are separate.
app.include_router(public_v1_router)
app.include_router(public_banner_router)
app.include_router(public_grievance_router)
app.include_router(public_rights_router)
