"""Model registry.

Importing every model here means Base.metadata is complete by the time Alembic
autogenerates, so a new table can never be silently left out of a migration.
"""

from app.models.api_key import ApiKey
from app.models.audit import AuditAction, AuditEvent
from app.models.tenant import Tenant
from app.models.user import RefreshToken, User

__all__ = [
    "ApiKey",
    "AuditAction",
    "AuditEvent",
    "RefreshToken",
    "Tenant",
    "User",
]

# Tables that hold customer data and therefore MUST have an RLS policy.
# The migration reads this list, and a test asserts every tenant-scoped table
# appears in it — so adding a table without a policy fails the build rather than
# leaking quietly.
TENANT_SCOPED_TABLES = [
    "users",
    "refresh_tokens",
    "api_keys",
    "audit_events",
]
