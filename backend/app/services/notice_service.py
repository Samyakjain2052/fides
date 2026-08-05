"""Purposes, notices, and the versioning rule that makes consent evidence.

The one idea worth holding onto: **published text never changes.** A draft is
editable; publishing freezes it; a change produces the next version. Consents
already recorded keep pointing at the version their signatory actually read.

The database enforces the freeze (a trigger, installed in migration 0002). This
module is the ergonomic path — `revise()` does the right thing so nobody has to
reach for an UPDATE and discover the trigger the hard way.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models.audit import AuditAction
from app.models.consent import LEGAL_BASES, Notice, Purpose
from app.services import audit_service
from app.services.audit_service import Actor

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class InvalidPurposeKey(Conflict):
    pass


# --------------------------------------------------------------------------- #
# Purposes
# --------------------------------------------------------------------------- #

async def create_purpose(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    key: str,
    name: str,
    category: str,
    is_mandatory: bool = False,
    legal_basis: str = "consent",
    retention_days: int | None = None,
) -> Purpose:
    key = (key or "").strip().lower()
    if not _KEY_RE.match(key):
        # The key goes into customers' integration code and into
        # /consent/check?purpose=…; a loose one becomes permanent the moment
        # somebody deploys against it.
        raise InvalidPurposeKey(
            "A purpose key must be lowercase letters, digits and underscores, "
            "start with a letter, and be 2–64 characters."
        )
    if legal_basis not in LEGAL_BASES:
        raise InvalidPurposeKey(f"Unknown legal basis {legal_basis!r}.")

    if await session.scalar(
        select(Purpose).where(Purpose.tenant_id == tenant_id, Purpose.key == key)
    ):
        raise Conflict(f"A purpose with key {key!r} already exists.")

    # A mandatory purpose resting on "consent" is a contradiction: consent that
    # cannot be refused is not consent, and presenting it as such is exactly the
    # dark pattern the DPDP Act is written against.
    if is_mandatory and legal_basis == "consent":
        raise InvalidPurposeKey(
            "A mandatory purpose cannot have 'consent' as its legal basis — "
            "consent that cannot be refused is not consent. Use "
            "'legal_obligation', 'legitimate_use' or 'vital_interest'."
        )

    purpose = Purpose(
        tenant_id=tenant_id,
        key=key,
        name=name.strip(),
        category=category.strip(),
        is_mandatory=is_mandatory,
        legal_basis=legal_basis,
        retention_days=retention_days,
    )
    session.add(purpose)
    await session.flush()

    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action=AuditAction.TENANT_UPDATED,
        entity_type="purpose",
        entity_id=purpose.id,
        payload={"key": key, "name": purpose.name, "is_mandatory": is_mandatory,
                 "legal_basis": legal_basis, "retention_days": retention_days},
    )
    return purpose


async def list_purposes(
    session: AsyncSession, tenant_id: uuid.UUID, *, include_inactive: bool = False
) -> list[Purpose]:
    stmt = select(Purpose).where(Purpose.tenant_id == tenant_id)
    if not include_inactive:
        stmt = stmt.where(Purpose.is_active.is_(True))
    rows = await session.execute(stmt.order_by(Purpose.name))
    return list(rows.scalars().all())


# --------------------------------------------------------------------------- #
# Notices
# --------------------------------------------------------------------------- #

async def draft_notice(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    purpose_id: uuid.UUID,
    content: str,
    data_collected: str,
    user_rights: str,
    withdrawal_policy: str,
    language: str = "English",
) -> Notice:
    """Create the next draft version for a purpose+language.

    Version numbers are per (purpose, language): the Hindi text and the English
    text of the same notice evolve independently, and forcing them to share a
    counter would make one of them lie about how many times it changed.
    """
    purpose = await session.scalar(
        select(Purpose).where(Purpose.id == purpose_id, Purpose.tenant_id == tenant_id)
    )
    if purpose is None:
        raise NotFound("No such purpose.")

    highest = await session.scalar(
        select(func.max(Notice.version)).where(
            Notice.tenant_id == tenant_id,
            Notice.purpose_id == purpose_id,
            Notice.language == language,
        )
    )

    notice = Notice(
        tenant_id=tenant_id,
        purpose_id=purpose_id,
        version=(highest or 0) + 1,
        language=language,
        content=content.strip(),
        data_collected=data_collected.strip(),
        user_rights=user_rights.strip(),
        withdrawal_policy=withdrawal_policy.strip(),
        published_at=None,
    )
    session.add(notice)
    await session.flush()
    return notice


async def publish_notice(
    session: AsyncSession, *, tenant_id: uuid.UUID, actor: Actor, notice_id: uuid.UUID
) -> Notice:
    """Freeze a draft. After this the text is evidence, and the trigger holds it."""
    notice = await session.scalar(
        select(Notice).where(Notice.id == notice_id, Notice.tenant_id == tenant_id)
    )
    if notice is None:
        raise NotFound("No such notice.")
    if notice.published_at is not None:
        return notice  # Idempotent.

    notice.published_at = datetime.now(UTC)
    notice.published_by = actor.id if actor.type == "user" else None
    await session.flush()

    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action=AuditAction.NOTICE_PUBLISHED,
        entity_type="notice",
        entity_id=notice.id,
        payload={
            "purpose_id": str(notice.purpose_id),
            "version": notice.version,
            "language": notice.language,
            # The wording itself goes into the chain. If the row were ever lost
            # or tampered with, the hashed audit entry still carries what was
            # published — the trail can reconstruct the evidence.
            "content": notice.content,
        },
    )
    return notice


async def revise_notice(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    notice_id: uuid.UUID,
    content: str | None = None,
    data_collected: str | None = None,
    user_rights: str | None = None,
    withdrawal_policy: str | None = None,
) -> Notice:
    """Edit a draft in place, or supersede a published notice with a new draft.

    One entry point for "change this wording", so a caller never has to know
    whether the thing they are editing happens to be published. Attempting the
    edit directly would hit the database trigger, which is correct but a poor
    way to find out.
    """
    notice = await session.scalar(
        select(Notice).where(Notice.id == notice_id, Notice.tenant_id == tenant_id)
    )
    if notice is None:
        raise NotFound("No such notice.")

    fields = {
        "content": content,
        "data_collected": data_collected,
        "user_rights": user_rights,
        "withdrawal_policy": withdrawal_policy,
    }

    if notice.published_at is None:
        for name, value in fields.items():
            if value is not None:
                setattr(notice, name, value.strip())
        await session.flush()
        return notice

    return await draft_notice(
        session,
        tenant_id=tenant_id,
        actor=actor,
        purpose_id=notice.purpose_id,
        language=notice.language,
        content=(content if content is not None else notice.content),
        data_collected=(
            data_collected if data_collected is not None else notice.data_collected
        ),
        user_rights=(user_rights if user_rights is not None else notice.user_rights),
        withdrawal_policy=(
            withdrawal_policy
            if withdrawal_policy is not None
            else notice.withdrawal_policy
        ),
    )


async def list_notices(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    purpose_id: uuid.UUID | None = None,
    published_only: bool = False,
) -> list[Notice]:
    stmt = select(Notice).where(Notice.tenant_id == tenant_id)
    if purpose_id is not None:
        stmt = stmt.where(Notice.purpose_id == purpose_id)
    if published_only:
        stmt = stmt.where(Notice.published_at.is_not(None))
    rows = await session.execute(
        stmt.order_by(Notice.purpose_id, Notice.language, Notice.version.desc())
    )
    return list(rows.scalars().all())


# --------------------------------------------------------------------------- #
# Starter content
# --------------------------------------------------------------------------- #

# A new workspace with no purposes has a consent module that cannot do anything,
# which reads as broken rather than empty. These are the four a DPDP-scoped
# fiduciary almost always needs, published so consent can be collected against
# them on day one.
#
# This is *configuration*, not activity: purposes and the wording of notices.
# Deliberately no consents are seeded — a consent has to be an act by a person,
# and inventing one would be exactly the fabrication the preview banners exist
# to prevent.
DEFAULT_PURPOSES = [
    {
        "key": "account_creation",
        "name": "Account creation",
        "category": "Identity Data",
        "is_mandatory": True,
        "legal_basis": "legal_obligation",
        "retention_days": 1825,
        "content": "We collect your name, email address and phone number to create "
                   "and operate your account.",
        "data_collected": "Name, email address, phone number",
        "user_rights": "You may access, correct or erase this data at any time.",
        "withdrawal_policy": "This purpose is required to hold an account. Closing "
                             "your account withdraws it.",
    },
    {
        "key": "marketing_email",
        "name": "Marketing communications",
        "category": "Contact Data",
        "is_mandatory": False,
        "legal_basis": "consent",
        "retention_days": 730,
        "content": "We use your email address to send product updates and offers.",
        "data_collected": "Email address",
        "user_rights": "You may withdraw this consent at any time.",
        "withdrawal_policy": "Marketing email stops within 24 hours of withdrawal.",
    },
    {
        "key": "analytics",
        "name": "Product analytics",
        "category": "Usage Data",
        "is_mandatory": False,
        "legal_basis": "consent",
        "retention_days": 365,
        "content": "We use anonymised usage data to understand which features are "
                   "used and to improve the product.",
        "data_collected": "Page views, device type, approximate region",
        "user_rights": "You may withdraw this consent at any time.",
        "withdrawal_policy": "Analytics collection stops immediately on withdrawal.",
    },
    {
        "key": "kyc_verification",
        "name": "KYC verification",
        "category": "Sensitive Identity Data",
        "is_mandatory": True,
        "legal_basis": "legal_obligation",
        "retention_days": 2555,
        "content": "Where required by law, we verify your identity using "
                   "government-issued identifiers.",
        "data_collected": "Identity document number, verification result",
        "user_rights": "Retained under a statutory obligation; erasure rights are "
                       "limited while that obligation applies.",
        "withdrawal_policy": "Cannot be withdrawn while the statutory retention "
                             "period applies.",
    },
]


async def seed_default_purposes(
    session: AsyncSession, *, tenant_id: uuid.UUID, actor: Actor
) -> list[Purpose]:
    """Give a new workspace a usable starting point.

    Every notice is published immediately: an unpublished notice cannot collect
    consent, so seeding drafts would leave the same dead end in a subtler form.
    """
    created: list[Purpose] = []
    for spec in DEFAULT_PURPOSES:
        purpose = await create_purpose(
            session,
            tenant_id=tenant_id,
            actor=actor,
            key=spec["key"],
            name=spec["name"],
            category=spec["category"],
            is_mandatory=spec["is_mandatory"],
            legal_basis=spec["legal_basis"],
            retention_days=spec["retention_days"],
        )
        notice = await draft_notice(
            session,
            tenant_id=tenant_id,
            actor=actor,
            purpose_id=purpose.id,
            content=spec["content"],
            data_collected=spec["data_collected"],
            user_rights=spec["user_rights"],
            withdrawal_policy=spec["withdrawal_policy"],
        )
        await publish_notice(
            session, tenant_id=tenant_id, actor=actor, notice_id=notice.id
        )
        created.append(purpose)
    return created
