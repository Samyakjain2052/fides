"""v1 router assembly."""

from fastapi import APIRouter

from app.api.v1 import admin, audit, auth, consent, dsar, retention

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(audit.router)
api_router.include_router(consent.router)
api_router.include_router(dsar.router)
api_router.include_router(retention.router)

__all__ = ["api_router"]
