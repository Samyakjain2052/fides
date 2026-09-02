"""
The audit trail — the product's core asset.

Design notes that are load-bearing, not decoration:

* **`seq` is per-tenant and monotonic.** Combined with `prev_hash`, that makes
  removal detectable: delete entry 7 and entry 8's `prev_hash` no longer matches
  entry 6's `hash`.
* **`hash` is an HMAC**, so an attacker with database write access still cannot
  recompute a valid chain — they would also need the signing key, which lives in
  the secret manager.
* **No `updated_at`.** The mixin is deliberately not used: an audit row that can
  record its own modification time is admitting it can be modified.
* **The migration revokes UPDATE and DELETE** from the application role and adds
  a trigger that raises on either. Grants stop the app; the trigger stops anyone
  who obtains the app's connection.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, UUIDMixin


class AuditEvent(UUIDMixin, TenantMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        # Two entries can never claim the same position in a tenant's chain.
        # Enforced by the database, not by hoping the advisory lock always held.
        UniqueConstraint("tenant_id", "seq", name="uq_audit_events_tenant_id_seq"),
        # The console filters by entity and by actor; both are indexed because
        # this table only ever grows.
        Index("ix_audit_events_entity", "tenant_id", "entity_type", "entity_id"),
        Index("ix_audit_events_actor", "tenant_id", "actor_id"),
        Index("ix_audit_events_action_created", "tenant_id", "action", "created_at"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # WHO. actor_type distinguishes a human from an API key from a scheduled job,
    # which is exactly the "initiator" column a regulator asks about.
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    actor_label: Mapped[str | None] = mapped_column(String(255))

    # WHAT. Verb-ish and stable: `consent.granted`, `dsar.completed`. Never a
    # free-text sentence — this is filtered and counted on.
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    # The facts. Whatever the event needs, as long as it is deterministic —
    # canonical_json() sorts keys so the hash is reproducible years later.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # WHERE FROM. Retained here rather than in application logs on purpose: it is
    # evidence, and it belongs under the tenant's retention policy.
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))

    # The chain.
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Database clock, not the application's: a skewed app server must not be able
    # to backdate evidence.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AuditEvent {self.seq} {self.action}>"


# Stable vocabulary for `action`. A closed set keeps the audit log queryable;
# ad-hoc strings make it prose.
class AuditAction:
    # auth
    LOGIN_SUCCEEDED = "auth.login_succeeded"
    LOGIN_FAILED = "auth.login_failed"
    LOGOUT = "auth.logout"
    TOKEN_REFRESHED = "auth.token_refreshed"
    TOKEN_REUSE_DETECTED = "auth.token_reuse_detected"
    ACCOUNT_LOCKED = "auth.account_locked"
    PASSWORD_CHANGED = "auth.password_changed"

    # tenant administration
    TENANT_CREATED = "tenant.created"
    TENANT_UPDATED = "tenant.updated"
    USER_CREATED = "user.created"
    USER_UPDATED = "user.updated"
    USER_ROLE_CHANGED = "user.role_changed"
    USER_DEACTIVATED = "user.deactivated"
    # Invitations. An invitation is a credential that grants account creation
    # with a chosen role, so issuing, accepting and revoking one are each facts
    # worth their own entry.
    INVITATION_SENT = "user.invitation_sent"
    INVITATION_ACCEPTED = "user.invitation_accepted"
    INVITATION_REVOKED = "user.invitation_revoked"
    SESSIONS_REVOKED = "user.sessions_revoked"

    APIKEY_CREATED = "apikey.created"
    APIKEY_REVOKED = "apikey.revoked"
    APIKEY_USED = "apikey.used"

    # consent (Phase 3)
    CONSENT_GRANTED = "consent.granted"
    CONSENT_WITHDRAWN = "consent.withdrawn"
    CONSENT_EXPIRED = "consent.expired"
    CONSENT_VALIDATED = "consent.validated"
    NOTICE_PUBLISHED = "notice.published"

    # rights requests (Phase 5)
    DSAR_SUBMITTED = "dsar.submitted"
    DSAR_STATUS_CHANGED = "dsar.status_changed"
    DSAR_COMPLETED = "dsar.completed"

    # grievances (Phase 6)
    GRIEVANCE_SUBMITTED = "grievance.submitted"
    GRIEVANCE_ASSIGNED = "grievance.assigned"
    GRIEVANCE_STATUS_CHANGED = "grievance.status_changed"
    GRIEVANCE_ESCALATED = "grievance.escalated"
    GRIEVANCE_RESOLVED = "grievance.resolved"
    GRIEVANCE_REOPENED = "grievance.reopened"
    GRIEVANCE_RATED = "grievance.rated"
    GRIEVANCE_OFFICER_CHANGED = "grievance.officer_changed"

    # retention
    #
    # These existed as behaviour before they existed as names: the purge
    # executor recorded its runs as `tenant.updated`, which is true in the
    # narrowest sense and useless in an audit. "Which entries show data being
    # destroyed?" had no answer you could filter for.
    RETENTION_POLICY_CREATED = "retention.policy_created"
    RETENTION_POLICY_UPDATED = "retention.policy_updated"
    RETENTION_PREVIEWED = "retention.previewed"
    RETENTION_PURGED = "retention.purged"

    # breaches (DPDP §8(6))
    BREACH_RECORDED = "breach.recorded"
    BREACH_UPDATED = "breach.updated"
    # Its own action, separate from BREACH_UPDATED, because this is the field the
    # whole statutory obligation hangs on and somebody will eventually litigate
    # when it changed and why.
    BREACH_DISCOVERY_CHANGED = "breach.discovery_changed"
    BREACH_AFFECTED_ATTACHED = "breach.affected_attached"
    BREACH_BOARD_NOTIFIED = "breach.board_notified"
    BREACH_PRINCIPALS_NOTIFIED = "breach.principals_notified"
    BREACH_CLOSED = "breach.closed"
    BREACH_VOIDED = "breach.voided"

    # reporting
    #
    # Worth auditing: "who extracted the consent register last quarter" is a
    # reasonable question to ask about a file full of personal data, and the
    # extract itself leaves no other trace because reports are never stored.
    REPORT_GENERATED = "report.generated"

    # integrity
    AUDIT_VERIFIED = "audit.verified"
    AUDIT_INTEGRITY_FAILED = "audit.integrity_failed"

    # Connections to a customer's own systems. Every one of these is recorded
    # because the payload is a live production credential: who added it, who
    # tested it, who deleted it, and whether the test passed. The credential
    # itself never appears in a payload — only its connector, label and the
    # verification outcome.
    CONNECTION_CREATED = "connection.created"
    CONNECTION_UPDATED = "connection.updated"
    CONNECTION_TESTED = "connection.tested"
    CONNECTION_DELETED = "connection.deleted"
