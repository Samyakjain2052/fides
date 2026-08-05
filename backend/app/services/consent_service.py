"""The consent lifecycle.

Every function here writes an audit entry, because the audit trail *is* the
consent history — there is no separate history table to drift out of step.

The invariants this module exists to hold:

* A consent is recorded against a **published notice version**, never a draft
  and never a bare purpose. Consenting to a draft would mean consenting to text
  that can still change.
* **Granting is explicit.** Nothing in here produces an `active` consent as a
  side effect or a default.
* **Withdrawing is one call**, no harder than granting (DPDP §6(4)).
* A **mandatory** purpose cannot be withdrawn through the ordinary path — but
  the caller is told why, rather than the control silently doing nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models.audit import AuditAction, AuditEvent
from app.models.publishable_key import ConsentProvenance
from app.models.consent import (
    CONSENT_METHODS,
    Consent,
    DataPrincipal,
    Notice,
    Purpose,
)
from app.services import audit_service
from app.services.audit_service import Actor


class ConsentRefused(Conflict):
    """A lawful reason the answer cannot be recorded as asked."""


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #

async def _get_purpose(session: AsyncSession, tenant_id: uuid.UUID, purpose_id: uuid.UUID) -> Purpose:
    purpose = await session.scalar(
        select(Purpose).where(Purpose.id == purpose_id, Purpose.tenant_id == tenant_id)
    )
    if purpose is None:
        raise NotFound("No such purpose.")
    return purpose


async def _get_principal(
    session: AsyncSession, tenant_id: uuid.UUID, principal_id: uuid.UUID
) -> DataPrincipal:
    principal = await session.scalar(
        select(DataPrincipal).where(
            DataPrincipal.id == principal_id, DataPrincipal.tenant_id == tenant_id
        )
    )
    if principal is None:
        raise NotFound("No such data principal.")
    return principal


async def current_notice(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    purpose_id: uuid.UUID,
    language: str = "English",
) -> Notice | None:
    """The highest published version for a purpose, in a language.

    Falls back to English when the requested language has no published notice.
    That fallback is a deliberate, visible compromise: showing English is worse
    than showing Hindi, but far better than either showing nothing or recording
    a consent against a notice the person could not read. The language actually
    used is stored on the consent, so the fallback is auditable rather than
    invisible.
    """
    stmt = (
        select(Notice)
        .where(
            Notice.tenant_id == tenant_id,
            Notice.purpose_id == purpose_id,
            Notice.language == language,
            Notice.published_at.is_not(None),
        )
        .order_by(Notice.version.desc())
        .limit(1)
    )
    notice = await session.scalar(stmt)
    if notice is not None or language == "English":
        return notice

    return await session.scalar(
        select(Notice)
        .where(
            Notice.tenant_id == tenant_id,
            Notice.purpose_id == purpose_id,
            Notice.language == "English",
            Notice.published_at.is_not(None),
        )
        .order_by(Notice.version.desc())
        .limit(1)
    )


# --------------------------------------------------------------------------- #
# Grant / withdraw
# --------------------------------------------------------------------------- #

async def grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    principal_id: uuid.UUID,
    purpose_id: uuid.UUID,
    language: str = "English",
    method: str = "checkbox",
    source: str | None = None,
    notice_id: uuid.UUID | None = None,
) -> Consent:
    """Record a yes.

    `notice_id` may be passed when the caller knows exactly which version was on
    screen — which is the honest thing for a UI to do, because the version the
    person actually read is the one that must be recorded, even if a newer one
    was published while they were reading. When omitted we resolve the current
    published version.
    """
    if method not in CONSENT_METHODS:
        raise ConsentRefused(f"Unknown consent method {method!r}.")

    purpose = await _get_purpose(session, tenant_id, purpose_id)
    if not purpose.is_active:
        raise ConsentRefused("That purpose is no longer collecting consent.")

    await _get_principal(session, tenant_id, principal_id)

    if notice_id is not None:
        notice = await session.scalar(
            select(Notice).where(Notice.id == notice_id, Notice.tenant_id == tenant_id)
        )
        if notice is None:
            raise NotFound("No such notice.")
        if notice.purpose_id != purpose_id:
            raise ConsentRefused("That notice belongs to a different purpose.")
    else:
        notice = await current_notice(session, tenant_id, purpose_id, language)

    if notice is None:
        # Refusing is the point. A consent with no notice behind it cannot
        # answer "to what, exactly?", which is the only question that matters
        # when it is challenged.
        raise ConsentRefused(
            "That purpose has no published notice, so consent cannot be recorded "
            "against it. Publish a notice first."
        )
    if notice.published_at is None:
        raise ConsentRefused("Consent cannot be recorded against an unpublished draft.")

    now = datetime.now(UTC)
    expires_at = (
        now + timedelta(days=purpose.retention_days) if purpose.retention_days else None
    )

    existing = await session.scalar(
        select(Consent).where(
            Consent.tenant_id == tenant_id,
            Consent.principal_id == principal_id,
            Consent.purpose_id == purpose_id,
        )
    )

    if existing is None:
        consent = Consent(
            tenant_id=tenant_id,
            principal_id=principal_id,
            purpose_id=purpose_id,
            notice_id=notice.id,
            status="active",
            given_at=now,
            expires_at=expires_at,
            language=notice.language,
            method=method,
            source=source,
        )
        session.add(consent)
    else:
        # Re-granting re-points at whatever version is current now: a fresh act
        # of consent is against the text shown at the time of that act.
        consent = existing
        consent.notice_id = notice.id
        consent.status = "active"
        consent.given_at = now
        consent.withdrawn_at = None
        consent.expires_at = expires_at
        consent.language = notice.language
        consent.method = method
        consent.source = source

    await session.flush()

    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action=AuditAction.CONSENT_GRANTED,
        entity_type="consent",
        entity_id=consent.id,
        payload={
            "principal_id": str(principal_id),
            "purpose_key": purpose.key,
            "notice_version": notice.version,
            "notice_id": str(notice.id),
            "language": notice.language,
            "method": method,
            "source": source,
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
    )
    return consent


async def withdraw(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    principal_id: uuid.UUID,
    purpose_id: uuid.UUID,
    reason: str | None = None,
) -> Consent:
    """Record a no. One call — withdrawing must be as easy as granting."""
    purpose = await _get_purpose(session, tenant_id, purpose_id)

    consent = await session.scalar(
        select(Consent).where(
            Consent.tenant_id == tenant_id,
            Consent.principal_id == principal_id,
            Consent.purpose_id == purpose_id,
        )
    )
    if consent is None:
        raise NotFound("No consent on record for that purpose.")

    if purpose.is_mandatory:
        # Say why, rather than failing silently or hiding the control. The
        # person is entitled to know that the processing continues and on what
        # basis — and to act on that knowledge by closing the account.
        raise ConsentRefused(
            f"{purpose.name} is required on the basis of "
            f"{purpose.legal_basis.replace('_', ' ')} and cannot be withdrawn "
            "while the account is open."
        )

    if consent.status == "withdrawn":
        return consent  # Idempotent: asking twice is not an error.

    consent.status = "withdrawn"
    consent.withdrawn_at = datetime.now(UTC)
    await session.flush()

    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action=AuditAction.CONSENT_WITHDRAWN,
        entity_type="consent",
        entity_id=consent.id,
        payload={
            "principal_id": str(principal_id),
            "purpose_key": purpose.key,
            "notice_id": str(consent.notice_id),
            "reason": reason,
        },
    )
    return consent


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

async def for_principal(
    session: AsyncSession, tenant_id: uuid.UUID, principal_id: uuid.UUID
) -> list[tuple[Consent, Purpose, Notice]]:
    """Every consent on record, with the purpose and the exact notice version."""
    rows = await session.execute(
        select(Consent, Purpose, Notice)
        .join(Purpose, Purpose.id == Consent.purpose_id)
        .join(Notice, Notice.id == Consent.notice_id)
        .where(Consent.tenant_id == tenant_id, Consent.principal_id == principal_id)
        .order_by(Purpose.name)
    )
    return list(rows.all())


async def check(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    purpose_key: str,
) -> dict[str, Any]:
    """"Do I have consent right now?" — the question the product exists to answer.

    Expiry is evaluated at read time rather than by a nightly job. A sweep that
    runs at 02:00 leaves a window in which an expired consent still reads as
    active, and processing during that window is unlawful. The row is left
    untouched; only the answer reflects the clock.
    """
    purpose = await session.scalar(
        select(Purpose).where(Purpose.tenant_id == tenant_id, Purpose.key == purpose_key)
    )
    if purpose is None:
        raise NotFound(f"No purpose with key {purpose_key!r}.")

    consent = await session.scalar(
        select(Consent).where(
            Consent.tenant_id == tenant_id,
            Consent.principal_id == principal_id,
            Consent.purpose_id == purpose.id,
        )
    )

    if consent is None:
        return {
            "allowed": False,
            "status": "never_given",
            "purpose": purpose.key,
            "reason": "No consent has been recorded for this purpose.",
        }

    now = datetime.now(UTC)
    expired = consent.expires_at is not None and consent.expires_at <= now
    status = "expired" if (expired and consent.status == "active") else consent.status

    return {
        "allowed": status == "active",
        "status": status,
        "purpose": purpose.key,
        "notice_version": None,
        "given_at": consent.given_at,
        "withdrawn_at": consent.withdrawn_at,
        "expires_at": consent.expires_at,
        "language": consent.language,
        "reason": {
            "active": None,
            "withdrawn": "The principal withdrew this consent.",
            "expired": "This consent has passed its retention period.",
        }.get(status),
    }


async def history(
    session: AsyncSession, tenant_id: uuid.UUID, principal_id: uuid.UUID
) -> list[AuditEvent]:
    """Consent history, read from the audit chain rather than a history table.

    One source of truth. A separate history table could disagree with the audit
    trail, and when those two disagree the product has nothing to sell.
    """
    rows = await session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.action.in_(
                [
                    AuditAction.CONSENT_GRANTED,
                    AuditAction.CONSENT_WITHDRAWN,
                    AuditAction.CONSENT_EXPIRED,
                ]
            ),
            AuditEvent.payload["principal_id"].astext == str(principal_id),
        )
        .order_by(AuditEvent.seq.desc())
    )
    return list(rows.scalars().all())


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #

async def record_provenance(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    consent: Consent,
    collection_method: str,
    strongly_bound: bool = False,
    origin: str | None = None,
    ip_hash: str | None = None,
    user_agent: str | None = None,
    notice_id: uuid.UUID | None = None,
    notice_version: int | None = None,
    publishable_key_id: uuid.UUID | None = None,
    api_key_id: uuid.UUID | None = None,
) -> ConsentProvenance:
    """Stamp where one act of consent came from, and put it in the audit chain.

    This is where trust in a publicly-collected record actually comes from. The
    key that collected it is published in a browser bundle, so the record cannot
    be trusted because of *who* submitted it — it is trusted because the server
    observed and recorded the circumstances, and those circumstances are in a
    tamper-evident chain.

    Every value here is server-derived. None of it is taken from the request body,
    because a client that could set its own provenance would be supplying its own
    alibi.
    """
    receipt = f"rcpt_{uuid.uuid4().hex}"
    now = datetime.now(UTC)

    row = ConsentProvenance(
        tenant_id=tenant_id,
        consent_id=consent.id,
        server_receipt_id=receipt,
        received_at=now,
        collection_method=collection_method,
        strongly_bound=strongly_bound,
        origin=origin,
        ip_hash=ip_hash,
        user_agent=(user_agent or "")[:1000] or None,
        notice_id=notice_id or consent.notice_id,
        notice_version=notice_version,
        publishable_key_id=publishable_key_id,
        api_key_id=api_key_id,
    )
    session.add(row)
    await session.flush()

    # Into the chain as its own entry. A provenance row that existed only in its
    # own table could be deleted with nothing to show it had been there; hashed
    # into the audit chain, its removal breaks the next entry's link.
    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action=AuditAction.CONSENT_GRANTED
        if consent.status == "active"
        else AuditAction.CONSENT_WITHDRAWN,
        entity_type="consent_provenance",
        entity_id=row.id,
        payload={
            "principal_id": str(consent.principal_id),
            "consent_id": str(consent.id),
            "server_receipt_id": receipt,
            "received_at": now.isoformat(),
            "collection_method": collection_method,
            "strongly_bound": strongly_bound,
            "origin": origin,
            "ip_hash": ip_hash,
            "user_agent": (user_agent or "")[:200] or None,
            "notice_version": notice_version,
            "publishable_key_id": str(publishable_key_id) if publishable_key_id else None,
        },
    )
    return row
