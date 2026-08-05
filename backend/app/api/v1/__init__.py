"""v1 router assembly."""

from fastapi import APIRouter

from app.api.v1 import admin, audit, auth, consent

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(audit.router)
api_router.include_router(consent.router)

__all__ = ["api_router"]
