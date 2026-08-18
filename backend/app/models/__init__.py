"""Model registry.

Importing every model here means Base.metadata is complete by the time Alembic
autogenerates, so a new table can never be silently left out of a migration.
"""

from app.models.api_key import ApiKey
from app.models.audit import AuditAction, AuditEvent
from app.models.breach import Breach, BreachAffectedPrincipal, BreachEvent
from app.models.consent import Consent, DataPrincipal, Notice, Purpose
from app.models.dsar import DsarEvent, DsarRequest
from app.models.invitation import UserInvitation
from app.models.grievance import Grievance, GrievanceEvent
from app.models.notification import Notification, NotificationTemplate
from app.models.retention import PurgeRun, PurgeRunItem, RetentionPolicy
from app.models.public_api import ApiRequestLog, IdempotencyKey
from app.models.publishable_key import ConsentProvenance, PublishableKey
from app.models.tenant import Tenant
from app.models.user import RefreshToken, User

__all__ = [
    "ApiKey",
    "ApiRequestLog",
    "AuditAction",
    "AuditEvent",
    "Breach",
    "BreachAffectedPrincipal",
    "BreachEvent",
    "Consent",
    "ConsentProvenance",
    "DsarEvent",
    "DsarRequest",
    "DataPrincipal",
    "Grievance",
    "GrievanceEvent",
    "IdempotencyKey",
    "Notice",
    "Notification",
    "NotificationTemplate",
    "PublishableKey",
    "PurgeRun",
    "PurgeRunItem",
    "Purpose",
    "RetentionPolicy",
    "RefreshToken",
    "Tenant",
    "UserInvitation",
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
    # Phase 3 — the consent domain.
    "purposes",
    "notices",
    "data_principals",
    "consents",
    # Phase 4 — the public API.
    "idempotency_keys",
    "api_request_log",
    # Publishable keys + provenance.
    "publishable_keys",
    "consent_provenance",
    # Phase 5 — rights requests.
    "dsar_requests",
    "dsar_events",
    # Phase 7 — retention.
    "retention_policies",
    "purge_runs",
    "purge_run_items",
    # Phase 8 — notifications.
    "notification_templates",
    "notifications",
    # Phase 6 — grievances.
    "grievances",
    "grievance_events",
    # Phase 9 — the breach register.
    "breaches",
    "breach_affected_principals",
    "breach_events",
    # Phase 8 — invitations.
    "user_invitations",
]
