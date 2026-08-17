"""The grievance lifecycle and the statutory escalation clock.

Read the model docstring first.

The one thing in this file that matters more than the rest: **the escalation
clock is evaluated on read, not only by a job.** A nightly sweep leaves a window
in which an overdue grievance still displays as fine, and that window is exactly
when a DPO is looking at the queue. So `sweep_escalations` runs when the queue is
listed, and `Grievance.escalation_due` is a computed property rather than a stored
flag. The row is the record; the display must not lag it.

The second thing: escalation is idempotent, at two levels. The `escalated` flag
means the sweep does no work twice, and the notification table's unique
constraint on (template, entity) means even a bug in the flag cannot double-notify
a Grievance Officer.
"""

from __future__ import annotations

import hashlib
import html
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models.audit import AuditAction
from app.models.consent import DataPrincipal
from app.models.grievance import (
    ALLOWED_TRANSITIONS,
    GRIEVANCE_CATEGORIES,
    Grievance,
    GrievanceEvent,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit_service, notification_service
from app.services.audit_service import Actor

logger = logging.getLogger("app.grievance")

# Free text from a member of the public. Capped at the service as well as in the
# form, because the form is not the only way in — the public endpoint is reachable
# by anything that can make an HTTP request.
MAX_DESCRIPTION = 8000

# How long a contact-confirmation link stays usable. Long enough for somebody who
# files a complaint on Friday evening, short enough that a leaked link in an old
# mailbox is not indefinitely useful.
VERIFICATION_TTL = timedelta(days=7)


class GrievanceRefused(Conflict):
    """A reason this grievance operation must not proceed."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

async def _next_reference(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    """GRV-2026-0007, per tenant, per year. Same reasoning as DSAR references."""
    year = datetime.now(UTC).year
    prefix = f"GRV-{year}-"
    used = (
        await session.scalar(
            select(func.count())
            .select_from(Grievance)
            .where(
                Grievance.tenant_id == tenant_id,
                Grievance.reference.startswith(prefix),
            )
        )
    ) or 0
    return f"{prefix}{used + 1:04d}"


def _hash_token(token: str) -> str:
    """Store the hash, mail the token.

    Same reasoning as every other credential in this product: a leaked database
    must not hand somebody the ability to confirm addresses they do not control.
    """
    return hashlib.sha256(token.encode()).hexdigest()


async def _tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
    if tenant is None:  # pragma: no cover - tenant context guarantees this
        raise NotFound("Unknown workspace.")
    return tenant


async def _event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    grievance: Grievance,
    actor: Actor,
    from_status: str | None,
    to_status: str | None,
    note: str | None = None,
    automated: bool = False,
) -> None:
    session.add(
        GrievanceEvent(
            tenant_id=tenant_id,
            grievance_id=grievance.id,
            actor_type=actor.type,
            actor_id=actor.id,
            actor_label=actor.label,
            from_status=from_status,
            to_status=to_status,
            note=note,
            automated=automated,
        )
    )
    await session.flush()


# --------------------------------------------------------------------------- #
# Filing
# --------------------------------------------------------------------------- #

async def file(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    category: str,
    description: str,
    principal_id: uuid.UUID | None = None,
    contact_email: str | None = None,
    related_dsar_id: uuid.UUID | None = None,
    require_verification: bool = False,
) -> tuple[Grievance, str | None]:
    """Record a grievance and start its clock.

    Returns the grievance and, when one was minted, the raw verification token —
    which the caller mails and never stores. This function does not send it,
    because the public and authenticated paths address the person differently and
    a service that guessed would get one of them wrong.

    The deadline and the escalation threshold both come from the tenant. A
    constant here would be wrong for every customer who promised faster, and
    silently wrong.
    """
    if category not in GRIEVANCE_CATEGORIES:
        raise GrievanceRefused(
            f"Unknown category {category!r}. One of: {', '.join(GRIEVANCE_CATEGORIES)}."
        )
    description = (description or "").strip()
    if len(description) < 10:
        raise GrievanceRefused(
            "Please describe the problem in at least a sentence. A complaint "
            "nobody can act on cannot be redressed."
        )
    if len(description) > MAX_DESCRIPTION:
        raise GrievanceRefused(
            f"The description is limited to {MAX_DESCRIPTION} characters."
        )
    if principal_id is None and not (contact_email or "").strip():
        # Also a CHECK constraint. Enforced here so the caller gets a sentence
        # rather than an integrity error.
        raise GrievanceRefused(
            "A grievance needs either an account or a contact address — "
            "otherwise there is no way to answer it."
        )

    tenant = await _tenant(session, tenant_id)
    now = datetime.now(UTC)

    token: str | None = None
    token_hash: str | None = None
    verified = False
    verified_at: datetime | None = None

    if principal_id is not None:
        # They are signed in. The address is already ours and already confirmed;
        # sending them a link to prove they own their own account would be theatre.
        verified = True
        verified_at = now
    elif require_verification:
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
    else:
        # Filed on somebody's behalf by staff (a phone call, a letter). The
        # address is unconfirmed and stays that way — recorded honestly rather
        # than marked verified because an employee vouched for it.
        pass

    grievance = Grievance(
        tenant_id=tenant_id,
        principal_id=principal_id,
        reference=await _next_reference(session, tenant_id),
        category=category,
        description=description,
        contact_email=(contact_email or "").strip() or None,
        contact_verified=verified,
        verification_token_hash=token_hash,
        verified_at=verified_at,
        status="open",
        related_dsar_id=related_dsar_id,
        submitted_at=now,
        deadline_at=now + timedelta(days=tenant.grievance_sla_days),
        escalate_at=now + timedelta(days=tenant.grievance_escalation_days),
    )
    session.add(grievance)
    await session.flush()

    await _event(
        session, tenant_id=tenant_id, grievance=grievance, actor=actor,
        from_status=None, to_status="open",
        note=f"Filed under {category}.",
    )
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.GRIEVANCE_SUBMITTED,
        entity_type="grievance", entity_id=grievance.id,
        payload={
            "reference": grievance.reference,
            "category": category,
            "deadline_at": grievance.deadline_at.isoformat(),
            "contact_verified": verified,
            # Deliberately NOT the description. The audit chain is read by people
            # investigating the platform, and copying a member of the public's
            # complaint — with whatever third parties they named in it — into a
            # second immutable table multiplies the exposure for no gain.
            "has_account": principal_id is not None,
        },
    )

    # One email, not two, and this service decides which. `grievance.confirm`
    # already carries the reference and the deadline, so sending an
    # acknowledgement as well would be the same information twice — and the
    # message asking the person to act would be the second one, which is the one
    # that gets ignored.
    #
    # Both sends live here rather than in the routers because the choice turns
    # entirely on whether a token was minted, which is decided above. Leaving one
    # of them to the caller meant a service-level test saw no email at all, and a
    # future caller would have had to remember.
    if token is None:
        await _notify_filed(session, tenant_id=tenant_id, grievance=grievance)
    else:
        await _notify_confirm(
            session, tenant_id=tenant_id, grievance=grievance, token=token
        )
    return grievance, token


async def _notify_filed(
    session: AsyncSession, *, tenant_id: uuid.UUID, grievance: Grievance
) -> None:
    to = await _reply_address(session, grievance)
    queued = await notification_service.enqueue(
        session,
        tenant_id=tenant_id,
        key="grievance.received",
        to_address=to,
        context={
            "reference": grievance.reference,
            "category": grievance.category.replace("_", " "),
            "deadline": grievance.deadline_at.date().isoformat(),
        },
        entity_type="grievance",
        entity_id=grievance.id,
        principal_id=grievance.principal_id,
    )
    await notification_service.send_now(session, notification=queued)


async def _notify_confirm(
    session: AsyncSession, *, tenant_id: uuid.UUID, grievance: Grievance, token: str
) -> None:
    """Ask an anonymous filer to confirm the address.

    The token travels in the message and is never stored — only its SHA-256 hash
    is, the same arrangement as every other credential here. It goes through the
    normal delivery log, so "we asked them to confirm and they never did" is on
    the record rather than an assumption.
    """
    queued = await notification_service.enqueue(
        session,
        tenant_id=tenant_id,
        key="grievance.confirm",
        to_address=grievance.contact_email,
        context={
            "reference": grievance.reference,
            "code": token,
            "deadline": grievance.deadline_at.date().isoformat(),
        },
        entity_type="grievance_confirmation",
        entity_id=grievance.id,
    )
    await notification_service.send_now(session, notification=queued)


async def _reply_address(session: AsyncSession, grievance: Grievance) -> str | None:
    """Where to write to about this grievance.

    The explicit contact address wins over the account's, because someone who
    typed a different address for a complaint probably meant it.
    """
    if grievance.contact_email:
        return grievance.contact_email
    if grievance.principal_id is None:
        return None
    principal = await session.scalar(
        select(DataPrincipal).where(DataPrincipal.id == grievance.principal_id)
    )
    return principal.email if principal else None


async def confirm_contact(
    session: AsyncSession, *, tenant_id: uuid.UUID, reference: str, token: str
) -> Grievance:
    """Confirm the address on a publicly-filed grievance.

    Compared with `secrets.compare_digest` against a stored hash, and the failure
    message does not distinguish "no such grievance" from "wrong token" — a
    confirmation endpoint that does becomes a way to enumerate references.
    """
    grievance = await session.scalar(
        select(Grievance).where(
            Grievance.tenant_id == tenant_id,
            Grievance.reference == reference,
        )
    )
    generic = GrievanceRefused(
        "That confirmation link is not valid. It may have expired, or already "
        "been used."
    )
    if grievance is None or grievance.verification_token_hash is None:
        raise generic
    if not secrets.compare_digest(
        grievance.verification_token_hash, _hash_token(token)
    ):
        raise generic
    if datetime.now(UTC) - grievance.submitted_at > VERIFICATION_TTL:
        raise generic

    grievance.contact_verified = True
    grievance.verified_at = datetime.now(UTC)
    # Single use. The link is now spent whether or not the mailbox keeps it.
    grievance.verification_token_hash = None
    await session.flush()

    actor = Actor(type="data_principal", id=None, label=grievance.contact_email)
    await _event(
        session, tenant_id=tenant_id, grievance=grievance, actor=actor,
        from_status=grievance.status, to_status=grievance.status,
        note="Contact address confirmed.",
    )
    return grievance


# --------------------------------------------------------------------------- #
# Throttling anonymous filing
# --------------------------------------------------------------------------- #
#
# The public endpoint takes no credential — deliberately, because §13 is a right
# and a key you must obtain first is a barrier. That makes it the one write path
# in this product an anonymous caller can reach, so it needs a limit.
#
# The limit is built from data already on the table rather than from stored client
# IPs. Logging the IP of everyone who files a privacy complaint, in order to
# protect the privacy complaint system, would be a poor trade.

# One unverified complaint per address at a time, and a ceiling on how many
# unconfirmed ones a workspace can accumulate in an hour. The first stops somebody
# being buried under complaints filed in their name; the second stops the queue
# being flooded at all.
UNVERIFIED_WINDOW = timedelta(hours=1)
MAX_UNVERIFIED_PER_WINDOW = 20


class TooManyGrievances(Conflict):
    """Anonymous filing is being used faster than a human could mean it."""


async def throttle_anonymous_filing(
    session: AsyncSession, *, tenant_id: uuid.UUID, contact_email: str
) -> None:
    existing = await session.scalar(
        select(func.count())
        .select_from(Grievance)
        .where(
            Grievance.tenant_id == tenant_id,
            Grievance.contact_email == contact_email,
            Grievance.contact_verified.is_(False),
            Grievance.status.notin_(("resolved", "rejected")),
        )
    )
    if existing:
        raise TooManyGrievances(
            "There is already a complaint from this address waiting to be "
            "confirmed. Please use the link in that email, or contact the "
            "Grievance Officer directly."
        )

    recent = await session.scalar(
        select(func.count())
        .select_from(Grievance)
        .where(
            Grievance.tenant_id == tenant_id,
            Grievance.contact_verified.is_(False),
            Grievance.submitted_at >= datetime.now(UTC) - UNVERIFIED_WINDOW,
        )
    )
    if (recent or 0) >= MAX_UNVERIFIED_PER_WINDOW:
        # Deliberately not a permanent refusal, and deliberately not silent: the
        # tenant's DPO can see the pile of unconfirmed rows in the queue.
        raise TooManyGrievances(
            "This organisation has received an unusual number of unconfirmed "
            "complaints in the last hour and is not accepting more right now. "
            "Please try again shortly, or write to the Grievance Officer."
        )


# --------------------------------------------------------------------------- #
# Working it
# --------------------------------------------------------------------------- #

async def get(
    session: AsyncSession, tenant_id: uuid.UUID, grievance_id: uuid.UUID
) -> Grievance:
    row = await session.scalar(
        select(Grievance).where(
            Grievance.tenant_id == tenant_id, Grievance.id == grievance_id
        )
    )
    if row is None:
        raise NotFound("No such grievance.")
    return row


async def change_status(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    grievance: Grievance,
    to_status: str,
    resolution_notes: str | None = None,
    rejection_reason: str | None = None,
    note: str | None = None,
) -> Grievance:
    """Move a grievance, refusing anything the state machine does not allow.

    Resolution notes and rejection reasons are required *here* as well as by the
    database. The CHECK is the guarantee; this is the sentence a human reads
    instead of an integrity error.
    """
    allowed = ALLOWED_TRANSITIONS.get(grievance.status, set())
    if to_status not in allowed:
        raise GrievanceRefused(
            f"A grievance that is {grievance.status} cannot become {to_status}. "
            + (
                f"Allowed from here: {', '.join(sorted(allowed))}."
                if allowed
                else "This grievance is closed."
            )
        )

    if to_status == "resolved" and not (resolution_notes or "").strip():
        raise GrievanceRefused(
            "Resolving a grievance requires a note saying how it was resolved. "
            "A redressal mechanism that records no redress is not one."
        )
    if to_status == "rejected" and not (rejection_reason or "").strip():
        raise GrievanceRefused(
            "Rejecting a grievance requires a reason. This is the point at which "
            "the person's next step is the Data Protection Board, and they are "
            "entitled to know why."
        )

    previous = grievance.status
    now = datetime.now(UTC)
    grievance.status = to_status

    if to_status == "acknowledged" and grievance.acknowledged_at is None:
        grievance.acknowledged_at = now
    if to_status == "resolved":
        grievance.resolution_notes = resolution_notes.strip()
        grievance.resolved_at = now
    if to_status == "rejected":
        grievance.rejection_reason = rejection_reason.strip()
        grievance.resolved_at = now
    if to_status == "reopened":
        # The clock does NOT restart. A reopened grievance is the same complaint,
        # still unanswered; restarting it would let an unsatisfactory resolution
        # buy another full statutory window.
        grievance.resolved_at = None

    await session.flush()
    await _event(
        session, tenant_id=tenant_id, grievance=grievance, actor=actor,
        from_status=previous, to_status=to_status, note=note,
    )

    action = {
        "resolved": AuditAction.GRIEVANCE_RESOLVED,
        "reopened": AuditAction.GRIEVANCE_REOPENED,
    }.get(to_status, AuditAction.GRIEVANCE_STATUS_CHANGED)
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor, action=action,
        entity_type="grievance", entity_id=grievance.id,
        payload={
            "reference": grievance.reference,
            "from": previous,
            "to": to_status,
            "days_open": grievance.days_open,
            "was_overdue": now > grievance.deadline_at,
        },
    )

    # Only outcomes are notified — same rule as DSAR. A message for every
    # internal step trains people to ignore the one that matters.
    if to_status in ("resolved", "rejected"):
        await _notify_outcome(session, tenant_id=tenant_id, grievance=grievance)
    return grievance


async def _notify_outcome(
    session: AsyncSession, *, tenant_id: uuid.UUID, grievance: Grievance
) -> None:
    to = await _reply_address(session, grievance)
    # Two templates, not one with a conditional sentence. "Your complaint is
    # resolved" arriving about a complaint that was refused is a wording that
    # reads as a resolution and is not one — and the person's next step (the
    # Board) depends on them understanding which happened.
    if grievance.status == "resolved":
        key = "grievance.resolved"
        # Rendered escaped by notification_service.render. Staff wrote the note
        # and it may quote the complainant, so it is not trusted into an email.
        context = {"reference": grievance.reference,
                   "resolution": grievance.resolution_notes}
    else:
        key = "grievance.rejected"
        context = {"reference": grievance.reference,
                   "reason": grievance.rejection_reason}
    queued = await notification_service.enqueue(
        session,
        tenant_id=tenant_id,
        key=key,
        to_address=to,
        context=context,
        entity_type="grievance_outcome",
        entity_id=grievance.id,
        principal_id=grievance.principal_id,
    )
    await notification_service.send_now(session, notification=queued)


async def assign(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    grievance: Grievance,
    user_id: uuid.UUID | None,
) -> Grievance:
    """Assign to a user in this tenant, or unassign with None.

    The user is looked up under tenant context, so RLS makes cross-tenant
    assignment impossible rather than merely discouraged.
    """
    if user_id is not None:
        assignee = await session.scalar(
            select(User).where(User.tenant_id == tenant_id, User.id == user_id)
        )
        if assignee is None:
            raise NotFound("No such user in this workspace.")
        if not assignee.is_active:
            raise GrievanceRefused(
                "That account is deactivated. Assigning a statutory complaint to "
                "somebody who cannot sign in is how a deadline passes unnoticed."
            )
        label = assignee.email
    else:
        label = None

    grievance.assigned_to = user_id
    await session.flush()
    await _event(
        session, tenant_id=tenant_id, grievance=grievance, actor=actor,
        from_status=grievance.status, to_status=grievance.status,
        note=f"Assigned to {label}." if label else "Unassigned.",
    )
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.GRIEVANCE_ASSIGNED,
        entity_type="grievance", entity_id=grievance.id,
        payload={"reference": grievance.reference, "assigned_to": str(user_id)
                 if user_id else None},
    )
    return grievance


async def rate(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    grievance: Grievance,
    rating: int,
    comment: str | None = None,
    reopen_if_unsatisfied: bool = True,
) -> Grievance:
    """The filer's verdict on the resolution.

    A low rating reopens the grievance by default, and that is the point of
    collecting one. A satisfaction score that goes into a dashboard and changes
    nothing is a metric; a score that reopens an unsatisfactory resolution is a
    redressal mechanism.
    """
    if grievance.status not in ("resolved", "reopened"):
        raise GrievanceRefused(
            "Only a resolved grievance can be rated — there is nothing to rate yet."
        )
    if not 1 <= rating <= 5:
        raise GrievanceRefused("A rating is between 1 and 5.")
    if grievance.satisfaction_rating is not None:
        raise GrievanceRefused(
            "This grievance has already been rated. If the resolution is still "
            "unsatisfactory, the next step is the Data Protection Board."
        )

    grievance.satisfaction_rating = rating
    grievance.satisfaction_comment = (comment or "").strip() or None
    await session.flush()

    await _event(
        session, tenant_id=tenant_id, grievance=grievance, actor=actor,
        from_status=grievance.status, to_status=grievance.status,
        note=f"Rated {rating}/5.",
    )
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor, action=AuditAction.GRIEVANCE_RATED,
        entity_type="grievance", entity_id=grievance.id,
        payload={"reference": grievance.reference, "rating": rating},
    )

    if reopen_if_unsatisfied and rating <= 2 and grievance.status == "resolved":
        await change_status(
            session, tenant_id=tenant_id, actor=actor, grievance=grievance,
            to_status="reopened",
            note=f"Reopened automatically: the filer rated the resolution {rating}/5.",
        )
    return grievance


# --------------------------------------------------------------------------- #
# The escalation clock
# --------------------------------------------------------------------------- #

async def escalate(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    grievance: Grievance,
    reason: str | None = None,
    automated: bool = False,
) -> Grievance:
    """Escalate to the Grievance Officer. Idempotent.

    What escalation *does*, deliberately: it flags the grievance, notifies the
    published Grievance Officer, and writes to the audit chain. What it does not
    do is contact the Data Protection Board. Automatic, unattended regulator
    contact is not something software should do — a person decides that, and the
    flag is what tells them to.
    """
    if grievance.escalated:
        # Not an error. Two workers, or a worker and a DPO's click, arriving at
        # the same conclusion is the system working.
        return grievance
    if not grievance.is_open:
        raise GrievanceRefused(
            "A closed grievance cannot be escalated. If the resolution was "
            "inadequate, reopen it."
        )

    now = datetime.now(UTC)
    grievance.escalated = True
    grievance.escalated_at = now
    await session.flush()

    await _event(
        session, tenant_id=tenant_id, grievance=grievance, actor=actor,
        from_status=grievance.status, to_status=grievance.status,
        note=reason or (
            f"Escalated automatically after {grievance.days_open} days without "
            "resolution."
            if automated else "Escalated."
        ),
        automated=automated,
    )
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.GRIEVANCE_ESCALATED,
        entity_type="grievance", entity_id=grievance.id,
        payload={
            "reference": grievance.reference,
            "days_open": grievance.days_open,
            "automated": automated,
            "escalate_at": grievance.escalate_at.isoformat(),
        },
    )

    tenant = await _tenant(session, tenant_id)
    # The officer's address is where this lands. It is set at registration to the
    # first admin, and changeable — but if it has been cleared, the notification
    # suppresses with a reason rather than vanishing, and the queue says so.
    queued = await notification_service.enqueue(
        session,
        tenant_id=tenant_id,
        key="grievance.escalated",
        to_address=tenant.grievance_officer_email,
        context={
            "reference": grievance.reference,
            "category": grievance.category.replace("_", " "),
            "days_open": grievance.days_open,
        },
        entity_type="grievance_escalation",
        entity_id=grievance.id,
    )
    await notification_service.send_now(session, notification=queued)
    return grievance


async def sweep_escalations(
    session: AsyncSession, *, tenant_id: uuid.UUID, limit: int = 200
) -> int:
    """Escalate everything past its threshold. Safe to call constantly.

    Called by the queue endpoint as well as by any scheduled job, because a
    nightly sweep leaves a window in which an overdue grievance still reads as
    fine — and that window is when somebody is looking.

    Idempotent three times over: the `escalated` flag filters the query, the
    `escalate` call re-checks it, and the notification's unique constraint would
    refuse a duplicate even if both failed.
    """
    now = datetime.now(UTC)
    rows = await session.execute(
        select(Grievance)
        .where(
            Grievance.tenant_id == tenant_id,
            Grievance.escalated.is_(False),
            Grievance.status.notin_(("resolved", "rejected")),
            Grievance.escalate_at < now,
            # An unconfirmed address does not get to page an officer. The
            # grievance is still recorded, still counted and still visible in the
            # queue — it just does not raise the statutory alarm on the strength
            # of an address nobody has proven they own.
            (Grievance.principal_id.isnot(None)) | (Grievance.contact_verified.is_(True)),
        )
        .order_by(Grievance.escalate_at)
        .limit(limit)
    )
    due = list(rows.scalars().all())
    if not due:
        return 0

    actor = Actor(type="system", id=None, label="escalation clock")
    count = 0
    for grievance in due:
        await escalate(
            session, tenant_id=tenant_id, actor=actor, grievance=grievance,
            automated=True,
        )
        count += 1
    logger.info(
        "escalated overdue grievances",
        extra={"context": {"tenant_id": str(tenant_id), "count": count}},
    )
    return count


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

async def list_for_tenant(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    status: str | None = None,
    category: str | None = None,
    assigned_to: uuid.UUID | None = None,
    escalated_only: bool = False,
    overdue_only: bool = False,
    limit: int = 100,
) -> list[Grievance]:
    stmt = select(Grievance).where(Grievance.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(Grievance.status == status)
    if category:
        stmt = stmt.where(Grievance.category == category)
    if assigned_to:
        stmt = stmt.where(Grievance.assigned_to == assigned_to)
    if escalated_only:
        stmt = stmt.where(Grievance.escalated.is_(True))
    if overdue_only:
        stmt = stmt.where(
            Grievance.deadline_at < datetime.now(UTC),
            Grievance.status.notin_(("resolved", "rejected")),
        )
    rows = await session.execute(
        # Oldest deadline first: the queue's order should be the order the
        # statutory risk arrives in, not the order things were filed.
        stmt.order_by(Grievance.deadline_at).limit(limit)
    )
    return list(rows.scalars().all())


async def list_for_principal(
    session: AsyncSession, tenant_id: uuid.UUID, principal_id: uuid.UUID
) -> list[Grievance]:
    rows = await session.execute(
        select(Grievance)
        .where(
            Grievance.tenant_id == tenant_id,
            Grievance.principal_id == principal_id,
        )
        .order_by(Grievance.submitted_at.desc())
    )
    return list(rows.scalars().all())


async def timeline(
    session: AsyncSession, tenant_id: uuid.UUID, grievance_id: uuid.UUID
) -> list[GrievanceEvent]:
    rows = await session.execute(
        select(GrievanceEvent)
        .where(
            GrievanceEvent.tenant_id == tenant_id,
            GrievanceEvent.grievance_id == grievance_id,
        )
        .order_by(GrievanceEvent.created_at)
    )
    return list(rows.scalars().all())


async def counts(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, int]:
    """Queue headline numbers, computed rather than stored.

    `overdue` is evaluated against the clock in this query, not read from a
    column, for the same reason `escalation_due` is a property.
    """
    now = datetime.now(UTC)
    open_statuses = ("open", "acknowledged", "in_progress", "reopened")
    total = await session.scalar(
        select(func.count()).select_from(Grievance)
        .where(Grievance.tenant_id == tenant_id)
    )
    open_count = await session.scalar(
        select(func.count()).select_from(Grievance).where(
            Grievance.tenant_id == tenant_id, Grievance.status.in_(open_statuses)
        )
    )
    overdue = await session.scalar(
        select(func.count()).select_from(Grievance).where(
            Grievance.tenant_id == tenant_id,
            Grievance.status.in_(open_statuses),
            Grievance.deadline_at < now,
        )
    )
    escalated = await session.scalar(
        select(func.count()).select_from(Grievance).where(
            Grievance.tenant_id == tenant_id,
            Grievance.escalated.is_(True),
            Grievance.status.in_(open_statuses),
        )
    )
    unverified = await session.scalar(
        select(func.count()).select_from(Grievance).where(
            Grievance.tenant_id == tenant_id,
            Grievance.status.in_(open_statuses),
            Grievance.principal_id.is_(None),
            Grievance.contact_verified.is_(False),
            Grievance.submitted_at >= now - VERIFICATION_TTL,
        )
    )
    # Separated from `awaiting_confirmation` because they need different actions.
    #
    # The confirmation window is shorter than the escalation threshold, so an
    # anonymous filing that is never confirmed becomes permanently unconfirmable
    # — and permanently unescalatable. Left in one bucket it would show up as a
    # slowly growing pile of things a DPO believes are still in flight. They are
    # not: nobody will ever click those links, and if the complaints are genuine
    # somebody has to pick them up by hand.
    stale = await session.scalar(
        select(func.count()).select_from(Grievance).where(
            Grievance.tenant_id == tenant_id,
            Grievance.status.in_(open_statuses),
            Grievance.principal_id.is_(None),
            Grievance.contact_verified.is_(False),
            Grievance.submitted_at < now - VERIFICATION_TTL,
        )
    )
    return {
        "total": total or 0,
        "open": open_count or 0,
        "overdue": overdue or 0,
        "escalated": escalated or 0,
        "awaiting_confirmation": unverified or 0,
        "confirmation_expired": stale or 0,
    }


# --------------------------------------------------------------------------- #
# The published Grievance Officer
# --------------------------------------------------------------------------- #

async def officer(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, object]:
    """Who a data principal is told to complain to.

    §13 requires this to be *published*. Defaulted at registration to the first
    admin so a new workspace is never non-compliant by omission, but it can be
    cleared — and if it is, this reports `published: false` so the screens can say
    so instead of showing a blank line where a statutory contact belongs.
    """
    tenant = await _tenant(session, tenant_id)
    name = (tenant.grievance_officer_name or "").strip()
    email = (tenant.grievance_officer_email or "").strip()
    return {
        "name": name or None,
        "email": email or None,
        "published": bool(name and email),
        "sla_days": tenant.grievance_sla_days,
        "escalation_days": tenant.grievance_escalation_days,
    }


async def set_officer(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    name: str,
    email: str,
    sla_days: int | None = None,
    escalation_days: int | None = None,
) -> dict[str, object]:
    """Change the published officer and, optionally, the clocks.

    Refuses an escalation threshold at or beyond the deadline: escalating on the
    day the statutory window closes gives the officer no time to act, which makes
    the escalation ceremonial.

    Changing the thresholds does **not** rewrite existing grievances. Their
    `deadline_at` and `escalate_at` were stamped at filing, and retroactively
    moving somebody's deadline because a setting changed today would make the
    record unreliable in exactly the direction that flatters the fiduciary.
    """
    tenant = await _tenant(session, tenant_id)
    name = (name or "").strip()
    email = (email or "").strip()
    if not name or not email:
        raise GrievanceRefused(
            "The Act requires a published Grievance Officer with a monitored "
            "address. Both a name and an email are needed."
        )

    new_sla = sla_days if sla_days is not None else tenant.grievance_sla_days
    new_esc = escalation_days if escalation_days is not None else (
        tenant.grievance_escalation_days
    )
    if not 1 <= new_sla <= 90:
        raise GrievanceRefused("The response window must be between 1 and 90 days.")
    if new_esc >= new_sla:
        raise GrievanceRefused(
            f"Escalation must happen before the deadline, not on or after it "
            f"({new_esc} vs {new_sla} days). An escalation with no time left to "
            "act on it is a formality."
        )

    before = {"name": tenant.grievance_officer_name,
              "email": tenant.grievance_officer_email,
              "sla_days": tenant.grievance_sla_days,
              "escalation_days": tenant.grievance_escalation_days}
    tenant.grievance_officer_name = name
    tenant.grievance_officer_email = email
    tenant.grievance_sla_days = new_sla
    tenant.grievance_escalation_days = new_esc
    await session.flush()

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.GRIEVANCE_OFFICER_CHANGED,
        entity_type="tenant", entity_id=tenant_id,
        payload={"before": before, "after": {"name": name, "email": email,
                                             "sla_days": new_sla,
                                             "escalation_days": new_esc}},
    )
    return await officer(session, tenant_id)


# --------------------------------------------------------------------------- #
# Rendering hostile text
# --------------------------------------------------------------------------- #

def safe_text(value: str | None) -> str:
    """Escape free text for any non-HTML-aware sink.

    The API returns `description` raw and React escapes it on render, which is
    the correct arrangement for the browser. This exists for the paths where
    nothing escapes on the way out — a PDF, a CSV, a plain-text email — where the
    absence of an escaping step is silent rather than visible.
    """
    return html.escape(value or "", quote=True)
