"""§14 nomination: recorded while somebody can, invoked only by a human.

The whole risk in this feature is in one operation. `invoke` transfers the
right to extract and erase a person's entire record to somebody else, on the
strength of a claim that the person has died or lost capacity. Getting that
wrong is not a compliance finding, it is a stranger emptying somebody's file.

So:

  * Nothing automates activation. No date, no external feed, no "if the account
    is inactive for two years" heuristic. A named member of staff records what
    evidence they saw, and the database refuses the state change without it.
  * The scope is chosen by the principal and enforced on every request the
    nominee raises. `access_only` is the default, because a relative settling an
    estate usually needs records and does not need the power to destroy them.
  * Revocation is unilateral, immediate, and needs no reason. §14 gives a right
    to nominate, not a right for the nominee to stay nominated.
  * Invoking is loudly audited, and so is every request made under it.

WHAT THIS DELIBERATELY DOES NOT DO

It does not verify a death certificate. Nothing here can, and a product that
implied otherwise would be worse than one that is honest about needing a human:
the failure mode of a fake verifier is somebody trusting it.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationProblem
from app.models.audit import AuditAction
from app.models.consent import DataPrincipal
from app.models.nomination import Nomination
from app.services import audit_service, notification_service
from app.services.audit_service import Actor

logger = logging.getLogger("app.nominations")
_settings = get_settings()


class NominationRefused(Conflict):
    """A procedural reason this cannot happen as asked."""


def _token_hash(secret: str) -> str:
    """Keyed, like every other lookup hash here.

    A plain SHA-256 of a token is brute-forceable from a stolen database; keying
    it with the JWT secret means an attacker needs the key as well.
    """
    return hmac.new(
        _settings.jwt_secret.encode(), secret.encode(), hashlib.sha256
    ).hexdigest()


async def create(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    principal_id: uuid.UUID,
    nominee_name: str,
    nominee_email: str,
    nominee_phone: str | None = None,
    nominee_relationship: str | None = None,
    scope: str = "access_only",
    instructions: str | None = None,
) -> tuple[Nomination, str]:
    """Record a nomination and return the acceptance token.

    Only one live nomination per principal. §14 says "any other individual",
    singular, and two live nominations would leave a fiduciary choosing between
    two people's instructions at the worst possible moment. Replacing one is
    explicit: revoke, then nominate again.
    """
    if scope not in ("access_only", "access_and_erasure", "all_rights"):
        raise ValidationProblem(f"Unknown scope {scope!r}.")

    name = (nominee_name or "").strip()
    email = (nominee_email or "").strip().lower()
    if not name:
        raise ValidationProblem("The nominee needs a name.")
    if "@" not in email:
        raise ValidationProblem(
            "The nominee needs an email address — they cannot be told the "
            "nomination exists otherwise, and a nominee who has never heard of "
            "it will not act on it."
        )

    principal = await session.scalar(
        select(DataPrincipal).where(DataPrincipal.id == principal_id)
    )
    if principal is None:
        raise NotFound("No such person.")

    existing = (
        await session.execute(
            select(Nomination).where(
                Nomination.principal_id == principal_id,
                Nomination.status.in_(("pending", "active", "invoked")),
            )
        )
    ).scalars().all()
    if existing:
        raise NominationRefused(
            "This person already has a nomination in force. §14 refers to one "
            "nominated individual; two would leave us choosing between their "
            "instructions at the worst possible moment. Revoke the existing "
            "one first."
        )

    secret = secrets.token_urlsafe(32)
    row = Nomination(
        tenant_id=tenant_id,
        principal_id=principal_id,
        nominee_name=name[:200],
        nominee_email=email[:320],
        nominee_phone=(nominee_phone or "").strip() or None,
        nominee_relationship=(nominee_relationship or "").strip() or None,
        scope=scope,
        status="pending",
        instructions=(instructions or "").strip() or None,
        acceptance_token_hash=_token_hash(secret),
    )
    session.add(row)
    await session.flush()

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.NOMINATION_CREATED,
        entity_type="nomination", entity_id=row.id,
        payload={
            "principal_id": str(principal_id),
            # The nominee's name and relationship, because who was authorised is
            # the point. Not the token, and not the phone number.
            "nominee_name": row.nominee_name,
            "nominee_relationship": row.nominee_relationship,
            "scope": scope,
        },
    )

    await notification_service.enqueue(
        session,
        tenant_id=tenant_id,
        key="nomination.recorded",
        to_address=row.nominee_email,
        entity_type="nomination",
        entity_id=row.id,
        context={
            "nominee_name": row.nominee_name,
            "scope": _SCOPE_WORDS[scope],
        },
    )
    return row, secret


_SCOPE_WORDS = {
    "access_only": "obtain a copy of their personal data",
    "access_and_erasure": "obtain a copy of their personal data, and ask for it to be erased",
    "all_rights": "exercise all of their rights, including correction and erasure",
}


async def accept(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    nomination_id: uuid.UUID,
    token: str,
) -> Nomination:
    """The nominee confirming they know about it.

    Moves `pending` to `active`. Acceptance is NOT what makes the nomination
    valid — §14 gives the right to the principal, and it does not depend on the
    nominee agreeing in advance — so a nomination that is never accepted stays
    usable. What acceptance buys is a nominee who knows.
    """
    row = await session.scalar(
        select(Nomination).where(Nomination.id == nomination_id)
    )
    if row is None or row.acceptance_token_hash is None:
        raise NotFound("No such nomination.")
    if not hmac.compare_digest(
        row.acceptance_token_hash, _token_hash(token or "")
    ):
        # Same generic refusal as every other token redemption here.
        raise NotFound("No such nomination.")
    if row.status not in ("pending", "active"):
        raise NominationRefused(
            f"That nomination is {row.status} and cannot be accepted."
        )

    row.status = "active"
    row.accepted_at = datetime.now(UTC)
    # Spent. Keeping the hash would leave a redeemable credential on a row that
    # no longer needs one.
    row.acceptance_token_hash = None
    return row


async def revoke(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    nomination: Nomination,
    note: str | None = None,
) -> Nomination:
    """Withdraw it. No reason required.

    Refuses only once invoked: at that point the principal is, by the premise of
    the invocation, no longer able to revoke — and letting staff quietly revoke
    an invoked nomination would be a way to shut out a legitimate nominee.
    Correcting a wrongly invoked one is `retract_invocation`, which says so.
    """
    if nomination.status == "invoked":
        raise NominationRefused(
            "This nomination has been invoked, so the person who made it is "
            "recorded as unable to revoke it. If it was invoked in error, "
            "retract the invocation — that is recorded as a correction rather "
            "than as the principal's own decision."
        )
    if nomination.status in ("revoked", "lapsed"):
        return nomination

    nomination.status = "revoked"
    nomination.revoked_at = datetime.now(UTC)
    nomination.revocation_note = (note or "").strip() or None
    nomination.acceptance_token_hash = None

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.NOMINATION_REVOKED,
        entity_type="nomination", entity_id=nomination.id,
        payload={
            "nominee_name": nomination.nominee_name,
            "had_status": "invoked" if nomination.invoked_at else "live",
        },
    )
    return nomination


async def invoke(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    nomination: Nomination,
    staff_user_id: uuid.UUID,
    evidence_note: str,
) -> Nomination:
    """Activate it, on evidence a human examined.

    The most consequential operation in this module and possibly in the
    product: after this, somebody other than the data principal can obtain or
    destroy their entire record.

    `evidence_note` is required by the service AND by a database CHECK, and it
    is required to be substantive rather than merely non-empty — "yes" is not a
    record of what was seen. Nothing about this is automated, because nothing
    can be: no software can establish that a person has died.
    """
    if nomination.status == "invoked":
        raise NominationRefused("That nomination is already in force.")
    if nomination.status in ("revoked", "lapsed"):
        raise NominationRefused(
            f"That nomination was {nomination.status} and cannot be invoked."
        )

    note = (evidence_note or "").strip()
    if len(note) < 20:
        raise ValidationProblem(
            "Record what evidence you saw — a death certificate and its number, "
            "a court order appointing a guardian, a medical certificate. This "
            "is the justification for letting somebody else act on another "
            "person's data, and it needs to read like one."
        )

    now = datetime.now(UTC)
    nomination.status = "invoked"
    nomination.invoked_at = now
    nomination.invoked_by = staff_user_id
    nomination.evidence_note = note

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.NOMINATION_INVOKED,
        entity_type="nomination", entity_id=nomination.id,
        payload={
            "principal_id": str(nomination.principal_id),
            "nominee_name": nomination.nominee_name,
            "scope": nomination.scope,
            "invoked_by": str(staff_user_id),
            # The evidence IS the record. It goes in the chain.
            "evidence": note[:2000],
        },
    )
    logger.warning(
        "nomination %s invoked for principal %s by user %s",
        nomination.id, nomination.principal_id, staff_user_id,
    )
    return nomination


async def retract_invocation(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    nomination: Nomination,
    reason: str,
) -> Nomination:
    """Undo an invocation made in error.

    Distinct from `revoke` on purpose. Revocation is the principal's decision;
    this is staff correcting their own mistake, and conflating the two would put
    a decision in the principal's name that they did not make — for a person
    who, by the premise of the invocation, could not have made it.

    Returns the nomination to `active`. The invocation stays in the audit chain:
    somebody was given access to another person's record, and that happened
    whether or not it should have.
    """
    if nomination.status != "invoked":
        raise NominationRefused("That nomination is not invoked.")

    text = (reason or "").strip()
    if not text:
        raise ValidationProblem(
            "Say why the invocation is being retracted. Somebody was given "
            "access to another person's record on the strength of it."
        )

    nomination.status = "active"
    nomination.invoked_at = None
    nomination.invoked_by = None
    nomination.evidence_note = None

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.NOMINATION_INVOCATION_RETRACTED,
        entity_type="nomination", entity_id=nomination.id,
        payload={
            "nominee_name": nomination.nominee_name,
            "reason": text[:2000],
        },
    )
    return nomination


async def for_principal(
    session, *, principal_id: uuid.UUID
) -> list[Nomination]:
    rows = await session.execute(
        select(Nomination)
        .where(Nomination.principal_id == principal_id)
        .order_by(Nomination.created_at.desc())
    )
    return list(rows.scalars().all())


async def live_for_principal(
    session, *, principal_id: uuid.UUID
) -> Nomination | None:
    return await session.scalar(
        select(Nomination).where(
            Nomination.principal_id == principal_id,
            Nomination.status.in_(("pending", "active", "invoked")),
        )
    )


async def get(session, *, nomination_id: uuid.UUID) -> Nomination:
    row = await session.scalar(
        select(Nomination).where(Nomination.id == nomination_id)
    )
    if row is None:
        raise NotFound("No such nomination.")
    return row


def authorise_request(
    *,
    nomination: Nomination,
    request_type: str,
) -> None:
    """Refuse unless this nomination lets the nominee raise this request.

    Two gates: the nomination must be invoked, and the request type must be
    inside the scope the principal chose. Both fail closed — `permits` returns
    False for an unknown type, so adding a new right without extending it
    refuses rather than quietly granting.
    """
    if not nomination.is_exercisable:
        raise PermissionDenied(
            f"That nomination is {nomination.status}. A nominee can only act "
            "once the nomination has been invoked, which requires somebody here "
            "to have seen evidence of death or incapacity."
        )
    if not nomination.permits(request_type):
        raise PermissionDenied(
            f"This nomination covers "
            f"{_SCOPE_WORDS.get(nomination.scope, nomination.scope)}, which does "
            f"not include a {request_type} request. The person who made it chose "
            "that limit."
        )


def as_dict(row: Nomination) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "principal_id": str(row.principal_id),
        "nominee_name": row.nominee_name,
        "nominee_email": row.nominee_email,
        "nominee_phone": row.nominee_phone,
        "nominee_relationship": row.nominee_relationship,
        "scope": row.scope,
        "scope_words": _SCOPE_WORDS.get(row.scope, row.scope),
        "status": row.status,
        "instructions": row.instructions,
        "accepted_at": row.accepted_at,
        "invoked_at": row.invoked_at,
        # The evidence note is returned to staff, because reviewing whether an
        # invocation was justified is the point of recording it.
        "evidence_note": row.evidence_note,
        "revoked_at": row.revoked_at,
        "is_live": row.is_live,
        "is_exercisable": row.is_exercisable,
        "created_at": row.created_at,
    }
