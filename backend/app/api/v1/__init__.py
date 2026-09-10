"""v1 router assembly."""

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    assessments,
    audit,
    auth,
    breaches,
    connections,
    consent,
    dsar,
    files,
    grievances,
    notifications,
    reports,
    retention,
    vendors,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(assessments.router)
api_router.include_router(audit.router)
api_router.include_router(breaches.router)
api_router.include_router(connections.router)
api_router.include_router(consent.router)
api_router.include_router(dsar.router)
api_router.include_router(files.router)
api_router.include_router(grievances.router)
api_router.include_router(notifications.router)
api_router.include_router(reports.router)
api_router.include_router(retention.router)
api_router.include_router(vendors.router)
