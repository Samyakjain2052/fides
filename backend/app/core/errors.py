"""
Errors as RFC 7807 problem+json, and a strict rule about leakage.

Every failure the API returns has the same shape, so clients parse one thing:

    {"type": "...", "title": "...", "status": 403, "detail": "...",
     "request_id": "..."}

The rule: an unexpected exception NEVER returns its message to the caller. Stack
traces and driver errors leak schema, file paths and sometimes data. The caller
gets a request id; the detail goes to the logs where the id can find it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_ctx

logger = logging.getLogger("app.errors")


class AppError(Exception):
    """Base for errors we raise deliberately and are happy to describe."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "about:blank"
    title = "Request failed"

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    error_type = "/errors/unauthenticated"
    title = "Not authenticated"


class PermissionDenied(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    error_type = "/errors/forbidden"
    title = "Not permitted"


class NotFound(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    error_type = "/errors/not-found"
    title = "Not found"


class Conflict(AppError):
    status_code = status.HTTP_409_CONFLICT
    error_type = "/errors/conflict"
    title = "Conflict"


class RateLimited(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_type = "/errors/rate-limited"
    title = "Too many requests"


class IntegrityViolation(AppError):
    """The audit chain failed verification. Loud on purpose."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_type = "/errors/integrity"
    title = "Audit integrity failure"


def _problem(status_code: int, error_type: str, title: str, detail: str, **extra: Any):
    body = {
        "type": error_type,
        "title": title,
        "status": status_code,
        "detail": detail,
        "request_id": request_id_ctx.get(),
        **extra,
    }
    return JSONResponse(
        status_code=status_code, content=body, media_type="application/problem+json"
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return _problem(exc.status_code, exc.error_type, exc.title, exc.detail, **exc.extra)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        return _problem(
            exc.status_code, "/errors/http", "HTTP error", str(exc.detail)
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        # Field-level detail is safe and useful; the submitted values are not
        # echoed back, because they may be personal data.
        fields = [
            {"field": ".".join(str(p) for p in e["loc"][1:]), "problem": e["msg"]}
            for e in exc.errors()
        ]
        return _problem(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "/errors/validation",
            "Validation failed",
            "One or more fields are invalid.",
            errors=fields,
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        logger.exception("unhandled exception: %s", type(exc).__name__)
        return _problem(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "/errors/internal",
            "Internal server error",
            "Something went wrong. Quote the request_id when reporting this.",
        )
