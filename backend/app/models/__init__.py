"""Model registry.

Importing every model here means Base.metadata is complete by the time Alembic
autogenerates, so a new table can never be silently left out of a migration.
"""

from app.models.api_key import ApiKey
from app.models.assessment import (
    Assessment,
    AssessmentAnswer,
    AssessmentTemplate,
    TemplateQuestion,
)
from app.models.audit import AuditAction, AuditEvent
from app.models.breach import Breach, BreachAffectedPrincipal, BreachEvent
from app.models.consent import Consent, DataPrincipal, Notice, Purpose
from app.models.dsar import DsarEvent, DsarRequest
from app.models.dsar_action_item import DsarActionItem
from app.models.dsar_message import DsarMessage
from app.models.invitation import UserInvitation
from app.models.job_run import JobRun
from app.models.grievance import Grievance, GrievanceEvent
from app.models.notification import Notification, NotificationTemplate
from app.models.retention import PurgeRun, PurgeRunItem, RetentionPolicy
from app.models.stored_file import StoredFile
from app.models.public_api import ApiRequestLog, IdempotencyKey
from app.models.publishable_key import ConsentProvenance, PublishableKey
from app.models.tenant import Tenant
from app.models.user import RefreshToken, User

__all__ = [
    "ApiKey",
    "ApiRequestLog",
    "Assessment",
    "AssessmentAnswer",
    "AssessmentTemplate",
    "AuditAction",
    "AuditEvent",
    "Breach",
    "BreachAffectedPrincipal",
    "BreachEvent",
    "Consent",
    "ConsentProvenance",
    "DsarActionItem",
    "DsarEvent",
    "DsarMessage",
    "DsarRequest",
    "DataPrincipal",
    "Grievance",
    "GrievanceEvent",
    "IdempotencyKey",
    "JobRun",
    "Notice",
    "Notification",
    "NotificationTemplate",
    "PublishableKey",
    "PurgeRun",
    "PurgeRunItem",
    "Purpose",
    "RetentionPolicy",
    "RefreshToken",
    "StoredFile",
    "TemplateQuestion",
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
    "dsar_messages",
    "dsar_action_items",
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
    # Connections to a customer's own systems.
    "connections",
    # Password resets. Was missing from this list for a release — it had a
    # policy, but the list did not know about it, which is precisely the drift
    # the note above claims cannot happen. It cannot now: the check queries
    # pg_class for every table carrying a tenant_id rather than trusting this.
    "password_resets",
    # Phase 10 — uploaded and generated objects. Metadata only; the bytes sit
    # in an object store, encrypted, and RLS here is what stops one customer
    # resolving another customer's file id to a downloadable object.
    "stored_files",
    # Phase 11 — assessments: DPIA (§10), RoPA, vendor reviews.
    "assessment_templates",
    "assessment_template_questions",
    "assessments",
    "assessment_answers",
]
