"""Recording rights requests that arrived as ordinary email.

The design decision worth defending is what this does NOT do: it does not read
an email and open a request.

"Please stop emailing me and remove my details" is either a withdrawal of
marketing consent or an erasure request under §12(3). The words are the same and
the consequences are not — one unsubscribes somebody, the other destroys their
record. Any classifier good enough to ship is wrong sometimes, and the cost of
being wrong in that direction is irreversible.

So: RECORD, REPLY, and let a human or the person themselves decide.

  record   the email, and crucially the date it arrived
  reply    once, pointing at the hosted form where the person chooses the right
           in the statute's own words
  link     a human connects it to a request when one exists, and the original
           arrival date travels with it

`suspected_type` exists so a busy inbox can be sorted, and is never acted on.
Keeping the hint and refusing to automate it is the honest version of both
things: the DPO gets the triage help, and nothing gets deleted on the strength
of a keyword.

WHY THE ARRIVAL DATE IS THE POINT

If somebody emails on the 1st and completes the form on the 20th, the fiduciary
has arguably been on notice since the 1st. A product that quietly restarted the
clock at form submission would be helping its customer understate how late they
are, which is the opposite of what a compliance tool is for.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.errors import Conflict, NotFound, ValidationProblem
from app.models.audit import AuditAction
from app.models.dsar import DsarRequest
from app.models.inbound_request import InboundRightsEmail
from app.services import audit_service, notification_service
from app.services.audit_service import Actor

logger = logging.getLogger("app.inbound")

#: Phrases that HINT at a request type. Used for sorting an inbox, never to
#: create anything — see the module docstring.
#:
#: Ordered most-specific first, and erasure last on purpose: the erasure
#: vocabulary overlaps heavily with unsubscribe language, so a message matching
#: both is reported as the less destructive guess.
_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("access", (
        r"\bcopy of my (personal )?data\b", r"\bsubject access\b",
        r"\bwhat (data|information) do you (hold|have)\b",
        r"\bsend me .{0,20}\bdata\b", r"\baccess request\b",
    )),
    ("correction", (
        r"\bincorrect\b", r"\bwrong\b", r"\bmisspel", r"\bcorrect my\b",
        r"\berror in my\b",
    )),
    ("updating", (
        r"\bchanged my\b", r"\bnew address\b", r"\bupdate my\b",
        r"\bmoved house\b", r"\bnew (phone|number)\b",
    )),
    ("completion", (
        r"\bmissing\b", r"\bincomplete\b", r"\byou (do not|don't) have my\b",
    )),
    ("erasure", (
        r"\berase\b", r"\bdelete (my|all my)\b", r"\bremove my (data|details|"
        r"information)\b", r"\bright to be forgotten\b", r"\bwipe my\b",
    )),
)


def suspect_type(subject: str | None, body: str | None) -> str | None:
    """A guess, offered as a hint and never acted on.

    Deliberately returns None rather than a default when nothing matches: an
    inbox where everything unmatched reads as "access" is worse than one where
    it reads as "unknown", because the second is honest about what it does not
    know.
    """
    text = f"{subject or ''}\n{body or ''}".lower()
    for kind, patterns in _HINTS:
        if any(re.search(p, text) for p in patterns):
            return kind
    return None


async def record(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    from_address: str,
    received_at: datetime | None = None,
    from_name: str | None = None,
    to_address: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    provider_message_id: str | None = None,
) -> tuple[InboundRightsEmail, bool]:
    """Record an inbound email. Returns (row, created).

    `created` is False for a duplicate delivery. Providers retry on any non-2xx,
    so a transient failure on our side would otherwise produce several copies of
    one person's email — and a DPO seeing an erasure request three times has no
    way to tell whether it is one person or three.
    """
    sender = (from_address or "").strip().lower()
    if "@" not in sender:
        raise ValidationProblem("An inbound email needs a sender address.")

    if provider_message_id:
        existing = await session.scalar(
            select(InboundRightsEmail).where(
                InboundRightsEmail.provider_message_id == provider_message_id
            )
        )
        if existing is not None:
            return existing, False

    row = InboundRightsEmail(
        tenant_id=tenant_id,
        from_address=sender[:320],
        from_name=(from_name or "").strip()[:200] or None,
        to_address=(to_address or "").strip().lower()[:320] or None,
        subject=(subject or "").strip()[:500] or None,
        body=body,
        # The date the EMAIL arrived, not the date we processed it. See the
        # module docstring — this is the date a regulator would count from.
        received_at=received_at or datetime.now(UTC),
        provider_message_id=provider_message_id,
        suspected_type=suspect_type(subject, body),
    )

    savepoint = await session.begin_nested()
    session.add(row)
    try:
        await savepoint.commit()
    except IntegrityError:
        # Two deliveries raced. The other one won; return it.
        await savepoint.rollback()
        existing = await session.scalar(
            select(InboundRightsEmail).where(
                InboundRightsEmail.provider_message_id == provider_message_id
            )
        )
        if existing is None:
            raise
        return existing, False

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.RIGHTS_EMAIL_RECEIVED,
        entity_type="inbound_rights_email", entity_id=row.id,
        payload={
            "from": row.from_address,
            "to": row.to_address,
            # The subject, because it is how a DPO recognises the message. Not
            # the body: it is unbounded text from a member of the public and
            # will routinely contain personal data about third parties.
            "subject": row.subject,
            "received_at": row.received_at.isoformat(),
            "suspected_type": row.suspected_type,
        },
    )
    return row, True


async def reply_with_form(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    inbound: InboundRightsEmail,
    form_url: str,
) -> InboundRightsEmail:
    """Point the person at the hosted form. Once.

    Once, because an auto-reply loop between two mail systems is a classic way
    to take both of them down, and because a person who emailed twice does not
    need two identical replies. `replied_at` is the guard.
    """
    if inbound.replied_at is not None:
        return inbound

    await notification_service.enqueue(
        session,
        tenant_id=tenant_id,
        key="rights.email_redirect",
        to_address=inbound.from_address,
        entity_type="inbound_rights_email",
        entity_id=inbound.id,
        context={
            "form_url": form_url,
            "received_on": f"{inbound.received_at:%d %B %Y}",
        },
    )
    inbound.replied_at = datetime.now(UTC)
    if inbound.status == "received":
        inbound.status = "replied"
    return inbound


async def link_to_request(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    inbound: InboundRightsEmail,
    request_id: uuid.UUID,
) -> InboundRightsEmail:
    """Attach an inbound email to the request it turned into.

    Does NOT move the request's deadline. Whether the clock should run from the
    email or from the form is a legal judgement about a specific message, and a
    tool that silently rewrote a statutory deadline either way would be making
    that judgement on the customer's behalf. What it does is make both dates
    visible on the request, so the decision is taken with the facts in view.
    """
    request = await session.scalar(
        select(DsarRequest).where(DsarRequest.id == request_id)
    )
    if request is None:
        raise NotFound("No such request.")

    inbound.linked_request_id = request.id
    inbound.status = "linked"

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.RIGHTS_EMAIL_LINKED,
        entity_type="inbound_rights_email", entity_id=inbound.id,
        payload={
            "reference": request.reference,
            "from": inbound.from_address,
            # Both dates, together, because the gap between them is the fact
            # somebody may have to explain.
            "email_received_at": inbound.received_at.isoformat(),
            "request_submitted_at": request.submitted_at.isoformat(),
            "days_between": (
                request.submitted_at - inbound.received_at
            ).days,
        },
    )
    return inbound


async def dismiss(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    inbound: InboundRightsEmail,
    reason: str,
    user_id: uuid.UUID | None = None,
) -> InboundRightsEmail:
    """Not a rights request after all — with a reason.

    A reason is required, and the database agrees. "We decided this was not a
    rights request" is a decision somebody may have to defend, and an inbox
    where things can be silently cleared is not a record of what arrived.
    """
    text = (reason or "").strip()
    if not text:
        raise ValidationProblem(
            "Say why this is not a rights request. Deciding that an email did "
            "not start a statutory clock is a decision somebody may be asked "
            "to justify."
        )
    if inbound.status == "linked":
        raise Conflict(
            "This email is linked to a request. Unlink it first if that was "
            "wrong."
        )

    inbound.status = "dismissed"
    inbound.dismissal_reason = text
    inbound.dismissed_by = user_id

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.RIGHTS_EMAIL_DISMISSED,
        entity_type="inbound_rights_email", entity_id=inbound.id,
        payload={
            "from": inbound.from_address,
            "subject": inbound.subject,
            "reason": text[:1000],
        },
    )
    return inbound


async def get(session, *, inbound_id: uuid.UUID) -> InboundRightsEmail:
    row = await session.scalar(
        select(InboundRightsEmail).where(InboundRightsEmail.id == inbound_id)
    )
    if row is None:
        raise NotFound("No such inbound email.")
    return row


async def listing(
    session, *, status: str | None = None
) -> list[InboundRightsEmail]:
    query = select(InboundRightsEmail)
    if status:
        query = query.where(InboundRightsEmail.status == status)
    rows = await session.execute(
        query.order_by(InboundRightsEmail.received_at.desc())
    )
    return list(rows.scalars().all())


def as_dict(row: InboundRightsEmail) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "from_address": row.from_address,
        "from_name": row.from_name,
        "to_address": row.to_address,
        "subject": row.subject,
        "body": row.body,
        "received_at": row.received_at,
        "status": row.status,
        "replied_at": row.replied_at,
        "linked_request_id": (
            str(row.linked_request_id) if row.linked_request_id else None
        ),
        "dismissal_reason": row.dismissal_reason,
        # Labelled as a guess everywhere it is rendered.
        "suspected_type": row.suspected_type,
        "is_open": row.is_open,
    }
