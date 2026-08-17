"""The breach lifecycle and the §8(6) notification duty.

Read the model docstring first.

Three things in this file carry the compliance weight:

**`discovered_at` changes are their own event.** Not folded into a generic update,
not changeable without a reason. It is the field the whole obligation hangs on, and
"when did you become aware" is the question a regulator opens with. Both the old
and the new value go into the audit chain, so a quiet backdating is visible.

**Notifying principals is resumable, and the progress figure is a count of rows.**
Ten thousand people, a provider rate limit, and a half-finished run is the normal
case rather than the exception. `breach_affected_principals.notified_at` is what
makes the second attempt safe, and every number the UI shows is
`SELECT count(*)` over that table — not a running total held in memory, which
would be wrong the moment anything failed.

**Closing requires the duty to be discharged, or an exemption in writing.** A
breach cannot reach `closed` with people un-notified unless somebody records why.
That exemption is a text field, not a boolean, because "we decided not to tell
them" is a decision that needs a sentence attached.

The product does not submit anything to the Data Protection Board. It generates
the content, and records that a named human submitted it and what reference they
got back. Unattended software contacting a regulator is not something this should
do, and pretending otherwise would be the most damaging lie in the module.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models.audit import AuditAction
from app.models.breach import (
    ALLOWED_TRANSITIONS,
    BOARD_NOTIFICATION_HOURS,
    SEVERITIES,
    Breach,
    BreachAffectedPrincipal,
    BreachEvent,
)
from app.models.consent import Consent, DataPrincipal, Purpose
from app.models.tenant import Tenant
from app.services import audit_service, notification_service
from app.services.audit_service import Actor

logger = logging.getLogger("app.breach")

# How many people one call to `notify_principals` will attempt.
#
# Bounded so a request cannot run for minutes and so a provider is not hammered
# in one burst. The caller loops, and because every attempt is recorded per row
# the loop is safe to stop and restart at any point.
NOTIFY_BATCH = 100


class BreachRefused(Conflict):
    """A reason this breach operation must not proceed."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

async def _next_reference(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    year = datetime.now(UTC).year
    prefix = f"BRE-{year}-"
    used = (
        await session.scalar(
            select(func.count())
            .select_from(Breach)
            .where(Breach.tenant_id == tenant_id, Breach.reference.startswith(prefix))
        )
    ) or 0
    return f"{prefix}{used + 1:04d}"


async def _event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    breach: Breach,
    actor: Actor,
    from_status: str | None,
    to_status: str | None,
    note: str | None = None,
    automated: bool = False,
) -> None:
    session.add(
        BreachEvent(
            tenant_id=tenant_id, breach_id=breach.id, actor_type=actor.type,
            actor_id=actor.id, actor_label=actor.label, from_status=from_status,
            to_status=to_status, note=note, automated=automated,
        )
    )
    await session.flush()


async def get(
    session: AsyncSession, tenant_id: uuid.UUID, breach_id: uuid.UUID
) -> Breach:
    row = await session.scalar(
        select(Breach).where(Breach.tenant_id == tenant_id, Breach.id == breach_id)
    )
    if row is None:
        raise NotFound("No such breach.")
    return row


def _require_open(breach: Breach) -> None:
    if breach.status == "void":
        raise BreachRefused(
            f"{breach.reference} was voided: {breach.void_reason}. A voided entry "
            "is kept as a record and cannot be worked on — record a new breach."
        )
    if breach.status == "closed":
        raise BreachRefused(
            f"{breach.reference} is closed. Reopening it would let the record of "
            "what was learned be rewritten; record a new breach instead."
        )


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #

async def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    title: str,
    description: str,
    severity: str = "medium",
    discovered_at: datetime | None = None,
    occurred_at: datetime | None = None,
    categories_affected: list[str] | None = None,
    estimated_affected_count: int | None = None,
) -> Breach:
    """Open a breach record, as a draft.

    Draft because the realistic first minute of an incident is partial
    information, and a form that refuses to save without every field is a form
    people work around by keeping notes elsewhere. `discovered_at` becomes
    mandatory the moment it leaves draft.
    """
    if severity not in SEVERITIES:
        raise BreachRefused(
            f"Unknown severity {severity!r}. One of: {', '.join(SEVERITIES)}."
        )
    if not (title or "").strip():
        raise BreachRefused("A breach needs a title somebody can recognise it by.")
    if not (description or "").strip():
        raise BreachRefused("A breach needs a description of what happened.")
    if occurred_at and discovered_at and occurred_at > discovered_at:
        raise BreachRefused(
            "The breach cannot have occurred after it was discovered. If the dates "
            "look that way, one of them is wrong."
        )

    breach = Breach(
        tenant_id=tenant_id,
        reference=await _next_reference(session, tenant_id),
        title=title.strip(),
        description=description.strip(),
        severity=severity,
        status="draft",
        discovered_at=discovered_at,
        occurred_at=occurred_at,
        categories_affected=categories_affected or [],
        estimated_affected_count=estimated_affected_count,
    )
    session.add(breach)
    await session.flush()

    await _event(
        session, tenant_id=tenant_id, breach=breach, actor=actor,
        from_status=None, to_status="draft", note=f"Recorded: {breach.title}",
    )
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor, action=AuditAction.BREACH_RECORDED,
        entity_type="breach", entity_id=breach.id,
        payload={
            "reference": breach.reference,
            "severity": severity,
            "discovered_at": discovered_at.isoformat() if discovered_at else None,
            "categories": breach.categories_affected,
        },
    )
    return breach


async def update(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    breach: Breach,
    discovered_at_reason: str | None = None,
    **fields: Any,
) -> Breach:
    """Update the narrative fields, and `discovered_at` only with a reason.

    A separate audit action and a mandatory reason for that one field, because it
    is the start of the statutory clock. Backdating awareness is the single most
    consequential edit anybody can make here, and it must not be possible to make
    it quietly.
    """
    _require_open(breach)

    editable = {
        "title", "description", "severity", "occurred_at", "contained_at",
        "categories_affected", "estimated_affected_count", "root_cause",
        "remediation",
    }
    changes: dict[str, Any] = {}

    if "discovered_at" in fields and fields["discovered_at"] is not None:
        new = fields.pop("discovered_at")
        old = breach.discovered_at
        if new != old:
            if old is not None and not (discovered_at_reason or "").strip():
                raise BreachRefused(
                    "Changing when you became aware of a breach needs a reason. "
                    "This is the field the notification deadline is measured from, "
                    "and an unexplained change to it is the first thing a regulator "
                    "will ask about."
                )
            occurred = fields.get("occurred_at", breach.occurred_at)
            if occurred and occurred > new:
                raise BreachRefused(
                    "That would place discovery before the breach occurred."
                )
            breach.discovered_at = new
            await session.flush()
            await _event(
                session, tenant_id=tenant_id, breach=breach, actor=actor,
                from_status=breach.status, to_status=breach.status,
                note=(
                    f"Awareness date changed from "
                    f"{old.isoformat() if old else '(unset)'} to {new.isoformat()}. "
                    f"Reason: {discovered_at_reason or 'first entry'}"
                ),
            )
            await audit_service.record(
                session, tenant_id=tenant_id, actor=actor,
                action=AuditAction.BREACH_DISCOVERY_CHANGED,
                entity_type="breach", entity_id=breach.id,
                payload={
                    "reference": breach.reference,
                    # Both values, so the chain shows the movement rather than just
                    # the destination.
                    "from": old.isoformat() if old else None,
                    "to": new.isoformat(),
                    "reason": discovered_at_reason,
                },
            )

    for key, value in fields.items():
        if key not in editable or value is None:
            continue
        if key == "severity" and value not in SEVERITIES:
            raise BreachRefused(f"Unknown severity {value!r}.")
        if getattr(breach, key) != value:
            changes[key] = value
            setattr(breach, key, value)

    if changes:
        await session.flush()
        await audit_service.record(
            session, tenant_id=tenant_id, actor=actor,
            action=AuditAction.BREACH_UPDATED,
            entity_type="breach", entity_id=breach.id,
            payload={"reference": breach.reference, "changed": sorted(changes)},
        )
    return breach


async def change_status(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    breach: Breach,
    to_status: str,
    note: str | None = None,
) -> Breach:
    """Move a breach through the register's states.

    `notified` is deliberately NOT reachable here — it is set by the notification
    steps themselves, once both halves of the duty are actually done. A status the
    UI can assert independently of the work is a status that will eventually be
    wrong.
    """
    if to_status == "notified":
        raise BreachRefused(
            "A breach becomes 'notified' by notifying — both the Board and the "
            "affected people. Use the notification steps; the status follows the "
            "work rather than the other way round."
        )
    if to_status == "void":
        raise BreachRefused("Voiding needs a reason. Use the void action.")
    if to_status == "closed":
        raise BreachRefused("Closing needs a root cause and remediation. Use close().")

    allowed = ALLOWED_TRANSITIONS.get(breach.status, set())
    if to_status not in allowed:
        raise BreachRefused(
            f"A breach that is {breach.status} cannot become {to_status}. "
            + (f"Allowed from here: {', '.join(sorted(allowed))}."
               if allowed else "This entry is final.")
        )
    if to_status != "draft" and breach.discovered_at is None:
        raise BreachRefused(
            "Record when you became aware of this breach first. Every deadline in "
            "§8(6) is measured from that moment, so nothing can leave draft "
            "without it."
        )

    previous = breach.status
    breach.status = to_status
    if to_status == "contained" and breach.contained_at is None:
        breach.contained_at = datetime.now(UTC)
    await session.flush()

    await _event(
        session, tenant_id=tenant_id, breach=breach, actor=actor,
        from_status=previous, to_status=to_status, note=note,
    )
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor, action=AuditAction.BREACH_UPDATED,
        entity_type="breach", entity_id=breach.id,
        payload={"reference": breach.reference, "from": previous, "to": to_status},
    )
    return breach


async def void(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    breach: Breach,
    reason: str,
) -> Breach:
    """Mark an entry as recorded in error. It is kept.

    There is no delete, anywhere in this module. A register whose entries can
    vanish is not a register — and "this was a mistake, here is why" is more
    useful to the next reader than an absence.
    """
    if not (reason or "").strip():
        raise BreachRefused(
            "Voiding an entry needs a reason. An entry that silently disappears "
            "is indistinguishable from one that was covered up."
        )
    if breach.status == "void":
        return breach
    if breach.status == "closed":
        raise BreachRefused(
            "A closed breach cannot be voided — it was investigated and resolved. "
            "If the record is wrong, correct it and say so in the timeline."
        )

    previous = breach.status
    breach.status = "void"
    breach.void_reason = reason.strip()
    await session.flush()

    await _event(
        session, tenant_id=tenant_id, breach=breach, actor=actor,
        from_status=previous, to_status="void", note=f"Voided: {reason.strip()}",
    )
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor, action=AuditAction.BREACH_VOIDED,
        entity_type="breach", entity_id=breach.id,
        payload={"reference": breach.reference, "from": previous, "reason": reason},
    )
    return breach


# --------------------------------------------------------------------------- #
# Who was affected
# --------------------------------------------------------------------------- #

async def find_by_categories(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    categories: list[str],
    limit: int = 5000,
) -> list[DataPrincipal]:
    """Principals holding a consent for a purpose in any of these categories.

    A saved query rather than a stored list, so a DPO can run it, look at exactly
    who it returns, and correct it before anything is sent. Notifying the wrong
    people about a breach is itself an incident.

    Excludes already-purged principals: their identifiers are masked, so there is
    nobody left to write to and including them would inflate the count.
    """
    if not categories:
        return []
    principal_ids = (
        select(Consent.principal_id)
        .join(Purpose, Purpose.id == Consent.purpose_id)
        .where(Consent.tenant_id == tenant_id, Purpose.category.in_(categories))
        .distinct()
    )
    rows = await session.execute(
        select(DataPrincipal)
        .where(
            DataPrincipal.tenant_id == tenant_id,
            DataPrincipal.id.in_(principal_ids),
            DataPrincipal.purged_at.is_(None),
        )
        .order_by(DataPrincipal.created_at)
        .limit(limit)
    )
    return list(rows.scalars().all())


async def attach_affected(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    breach: Breach,
    principal_ids: list[uuid.UUID],
    source: str = "query",
) -> dict[str, int]:
    """Add people to the affected list, skipping any already on it.

    Idempotent by constraint: `UNIQUE (breach_id, principal_id)` means running the
    same query twice adds nobody twice, which matters because both the notification
    count and the figure a regulator is shown come off this table.
    """
    _require_open(breach)
    if breach.principals_notified_at is not None:
        # Not forbidden, but it means the notified figure now understates the
        # affected total until the run is repeated. Worth being loud about.
        logger.warning(
            "principals attached after notification completed",
            extra={"context": {"breach": breach.reference, "added": len(principal_ids)}},
        )

    added = 0
    skipped = 0
    for principal_id in principal_ids:
        row = BreachAffectedPrincipal(
            tenant_id=tenant_id, breach_id=breach.id, principal_id=principal_id,
            source=source,
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
            added += 1
        except IntegrityError:
            # Already attached. The constraint doing its job, not an error.
            skipped += 1

    total = await affected_count(session, tenant_id=tenant_id, breach=breach)
    await _event(
        session, tenant_id=tenant_id, breach=breach, actor=actor,
        from_status=breach.status, to_status=breach.status,
        note=(
            f"Attached {added} affected data principal(s) via {source}"
            + (f"; {skipped} already listed" if skipped else "")
            + f". {total} on the list."
        ),
    )
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.BREACH_AFFECTED_ATTACHED,
        entity_type="breach", entity_id=breach.id,
        payload={
            "reference": breach.reference, "added": added, "already_listed": skipped,
            "total_affected": total, "source": source,
        },
    )
    return {"added": added, "already_listed": skipped, "total": total}


async def affected_count(
    session: AsyncSession, *, tenant_id: uuid.UUID, breach: Breach
) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(BreachAffectedPrincipal)
            .where(
                BreachAffectedPrincipal.tenant_id == tenant_id,
                BreachAffectedPrincipal.breach_id == breach.id,
            )
        )
    ) or 0


@dataclass
class NotifyProgress:
    """The truth about a bulk run, counted from rows rather than remembered.

    A running total held in memory is wrong the moment anything fails or the
    process restarts, and this is the number a DPO uses to decide whether the
    statutory duty has been discharged.
    """

    total: int
    notified: int
    suppressed: int
    remaining: int
    complete: bool

    @property
    def summary(self) -> str:
        # "4,812 of 10,000 notified" is the truth. A green tick at 48% is not.
        return f"{self.notified:,} of {self.total:,} notified"


async def notify_progress(
    session: AsyncSession, *, tenant_id: uuid.UUID, breach: Breach
) -> NotifyProgress:
    base = select(func.count()).select_from(BreachAffectedPrincipal).where(
        BreachAffectedPrincipal.tenant_id == tenant_id,
        BreachAffectedPrincipal.breach_id == breach.id,
    )
    total = (await session.scalar(base)) or 0
    handled = (
        await session.scalar(base.where(BreachAffectedPrincipal.notified_at.isnot(None)))
    ) or 0
    suppressed = (
        await session.scalar(
            base.where(BreachAffectedPrincipal.suppressed_reason.isnot(None))
        )
    ) or 0
    return NotifyProgress(
        total=total,
        # Delivered, not merely attempted: a suppressed row is handled but nobody
        # was told, and conflating the two would overstate the discharge.
        notified=handled - suppressed,
        suppressed=suppressed,
        remaining=total - handled,
        complete=total > 0 and handled == total,
    )


# --------------------------------------------------------------------------- #
# The notification duty
# --------------------------------------------------------------------------- #

def board_notification_content(breach: Breach, tenant: Tenant) -> str:
    """The text a human submits to the Board.

    Generated, not sent. There is no Board API — it is a portal process — and
    unattended software contacting a regulator is not something this product does.
    """
    lines = [
        "PERSONAL DATA BREACH NOTIFICATION",
        "Digital Personal Data Protection Act, 2023 — section 8(6)",
        "",
        f"Data Fiduciary        {tenant.name}"
        + (f" ({tenant.legal_name})" if tenant.legal_name and tenant.legal_name != tenant.name else ""),
        f"Internal reference    {breach.reference}",
        f"Severity              {breach.severity}",
        "",
        f"Became aware          {breach.discovered_at.isoformat() if breach.discovered_at else '(not recorded)'}",
        f"Occurred              {breach.occurred_at.isoformat() if breach.occurred_at else '(unknown)'}",
        f"Contained             {breach.contained_at.isoformat() if breach.contained_at else '(not yet)'}",
        "",
        f"Nature of the breach  {breach.title}",
        "",
        breach.description,
        "",
        "Categories of personal data affected",
        "  " + (", ".join(breach.categories_affected) or "(not recorded)"),
        "",
        f"Data principals affected (estimate)  {breach.estimated_affected_count if breach.estimated_affected_count is not None else '(not estimated)'}",
        "",
        "Root cause",
        "  " + (breach.root_cause or "(under investigation)"),
        "",
        "Remedial measures taken",
        "  " + (breach.remediation or "(in progress)"),
        "",
        "Grievance Officer",
        f"  {tenant.grievance_officer_name or '(not published)'}"
        f" — {tenant.grievance_officer_email or '(not published)'}",
        "",
        "--",
        "Generated by DataShield. This text is prepared for a human to submit; "
        "this system does not transmit anything to the Board.",
    ]
    return "\n".join(lines)


async def notify_board(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    breach: Breach,
    submitted_by: str,
    reference: str | None = None,
    submitted_at: datetime | None = None,
) -> Breach:
    """Record that a named human submitted the notification.

    Deliberately shaped as a record of somebody's action rather than as an action
    of its own. `submitted_by` is required for that reason: "the system reported
    it" would be false, and a compliance record whose most load-bearing claim is
    false is worse than no record.
    """
    _require_open(breach)
    if breach.discovered_at is None:
        raise BreachRefused(
            "Record when you became aware of this breach before notifying the "
            "Board — the notification itself has to state it."
        )
    if not (submitted_by or "").strip():
        raise BreachRefused(
            "Name the person who submitted this to the Board. The product does not "
            "submit anything itself, so the record has to say who did."
        )
    if breach.board_notified_at is not None:
        raise BreachRefused(
            f"The Board was already notified on "
            f"{breach.board_notified_at.date().isoformat()}"
            + (f" (reference {breach.board_reference})" if breach.board_reference else "")
            + ". Record a follow-up in the timeline rather than overwriting this."
        )

    when = submitted_at or datetime.now(UTC)
    if when < breach.discovered_at:
        raise BreachRefused(
            "The Board cannot have been notified before you became aware of the "
            "breach."
        )

    breach.board_notified_at = when
    breach.board_reference = (reference or "").strip() or None
    breach.board_submitted_by = submitted_by.strip()
    await session.flush()
    await _maybe_mark_notified(session, tenant_id=tenant_id, actor=actor, breach=breach)

    hours = (when - breach.discovered_at).total_seconds() / 3600
    await _event(
        session, tenant_id=tenant_id, breach=breach, actor=actor,
        from_status=breach.status, to_status=breach.status,
        note=(
            f"Board notified by {submitted_by.strip()}"
            + (f", reference {breach.board_reference}" if breach.board_reference else "")
            + f" — {hours:.1f}h after becoming aware."
        ),
    )
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.BREACH_BOARD_NOTIFIED,
        entity_type="breach", entity_id=breach.id,
        payload={
            "reference": breach.reference,
            "board_reference": breach.board_reference,
            "submitted_by": submitted_by.strip(),
            "hours_after_discovery": round(hours, 2),
            # Recorded so the judgement is on the record rather than recomputed
            # later against a threshold that may have changed.
            "within_threshold_hours": BOARD_NOTIFICATION_HOURS,
            "late": hours > BOARD_NOTIFICATION_HOURS,
        },
    )
    return breach


async def notify_principals(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    breach: Breach,
    batch: int = NOTIFY_BATCH,
) -> NotifyProgress:
    """Notify up to `batch` affected people. Safe to call repeatedly.

    The resumability is the whole design. Each row is claimed with
    `FOR UPDATE SKIP LOCKED` and stamped as it is handled, so two concurrent runs
    take disjoint sets and a run that dies halfway loses nothing but its place.

    A person with no address on record is stamped as suppressed with a reason
    rather than left unhandled — otherwise the run would never complete and the
    remaining count would be permanently wrong.
    """
    _require_open(breach)
    if breach.discovered_at is None:
        raise BreachRefused("Record when you became aware of this breach first.")
    if not breach.remediation:
        raise BreachRefused(
            "Say what you have done about it before writing to the people "
            "affected. A notice that describes a breach and offers no remedy tells "
            "somebody they have a problem and nothing they can do about it."
        )

    total = await affected_count(session, tenant_id=tenant_id, breach=breach)
    if total == 0:
        raise BreachRefused(
            "Nobody is on the affected list yet. Attach the affected data "
            "principals first, and review who they are before anything is sent."
        )

    rows = await session.execute(
        select(BreachAffectedPrincipal)
        .where(
            BreachAffectedPrincipal.tenant_id == tenant_id,
            BreachAffectedPrincipal.breach_id == breach.id,
            BreachAffectedPrincipal.notified_at.is_(None),
        )
        .order_by(BreachAffectedPrincipal.created_at)
        .limit(batch)
        .with_for_update(skip_locked=True)
    )
    claimed = list(rows.scalars().all())

    discovered = breach.discovered_at.date().isoformat()
    categories = ", ".join(breach.categories_affected) or "personal data we hold"
    now = datetime.now(UTC)

    for row in claimed:
        principal = await session.scalar(
            select(DataPrincipal).where(DataPrincipal.id == row.principal_id)
        )
        address = principal.email if principal else None
        queued = await notification_service.enqueue(
            session,
            tenant_id=tenant_id,
            key="breach.principal_notice",
            to_address=address,
            context={
                "reference": breach.reference,
                "categories": categories,
                "discovered_on": discovered,
                "remediation": breach.remediation,
            },
            entity_type="breach_principal",
            # Keyed on the join row, so the notification table's own uniqueness
            # constraint independently prevents telling one person twice about one
            # breach — belt and braces with `notified_at`.
            entity_id=row.id,
            principal_id=row.principal_id,
        )
        sent = await notification_service.send_now(session, notification=queued)

        row.notified_at = now
        if sent is None:
            # Already queued for this row on an earlier attempt. Treated as handled
            # so the run can finish; the delivery log holds what happened.
            row.suppressed_reason = "already queued on an earlier run"
        elif sent.status == "suppressed":
            row.suppressed_reason = sent.suppression_reason
            row.notification_id = sent.id
        else:
            row.notification_id = sent.id
        await session.flush()

    progress = await notify_progress(session, tenant_id=tenant_id, breach=breach)

    if progress.complete and breach.principals_notified_at is None:
        breach.principals_notified_at = now
        await session.flush()
        await _maybe_mark_notified(
            session, tenant_id=tenant_id, actor=actor, breach=breach
        )

    await _event(
        session, tenant_id=tenant_id, breach=breach, actor=actor,
        from_status=breach.status, to_status=breach.status,
        note=(
            f"Notification run: {len(claimed)} attempted. {progress.summary}"
            + (f", {progress.suppressed} could not be reached" if progress.suppressed else "")
            + ("." if progress.complete else f", {progress.remaining:,} remaining.")
        ),
    )
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.BREACH_PRINCIPALS_NOTIFIED,
        entity_type="breach", entity_id=breach.id,
        payload={
            "reference": breach.reference,
            "attempted_this_run": len(claimed),
            "notified": progress.notified,
            "suppressed": progress.suppressed,
            "remaining": progress.remaining,
            "complete": progress.complete,
        },
    )
    return progress


async def _maybe_mark_notified(
    session: AsyncSession, *, tenant_id: uuid.UUID, actor: Actor, breach: Breach
) -> None:
    """Advance to `notified` only when BOTH halves of the duty are done.

    The status follows the work. A CHECK constraint enforces the same rule at the
    database, so this is the friendly path to a guarantee that holds either way.
    """
    if breach.board_notified_at is None or breach.principals_notified_at is None:
        return
    if breach.status in ("notified", "closed", "void"):
        return

    previous = breach.status
    breach.status = "notified"
    await session.flush()
    await _event(
        session, tenant_id=tenant_id, breach=breach, actor=actor,
        from_status=previous, to_status="notified",
        note="Both the Board and every affected data principal have now been notified.",
        automated=True,
    )


async def close(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    breach: Breach,
    root_cause: str,
    remediation: str,
    notification_exemption: str | None = None,
) -> Breach:
    """Close a breach, with the cause and the fix on the record.

    Refuses while people remain un-notified unless an exemption is written down.
    The exemption is text rather than a flag because "we decided not to tell them"
    is a decision that needs a sentence attached — and that sentence is what
    somebody will be asked to justify.
    """
    _require_open(breach)
    if not (root_cause or "").strip() or not (remediation or "").strip():
        raise BreachRefused(
            "Closing a breach requires a root cause and what was done about it. A "
            "closed breach with no cause recorded teaches nobody anything, and the "
            "next one will be the same breach."
        )
    if breach.board_notified_at is None:
        raise BreachRefused(
            "The Data Protection Board has not been notified. That is a statutory "
            "obligation and closing without it would record compliance that did "
            "not happen."
        )

    progress = await notify_progress(session, tenant_id=tenant_id, breach=breach)
    exemption = (notification_exemption or "").strip()
    if not progress.complete and not exemption:
        raise BreachRefused(
            f"{progress.remaining:,} affected data principal(s) have not been "
            "notified. Notifying them is a separate obligation from notifying the "
            "Board. Finish the run, or record a written exemption explaining why "
            "they will not be told."
        )

    previous = breach.status
    breach.status = "closed"
    breach.root_cause = root_cause.strip()
    breach.remediation = remediation.strip()
    breach.closed_at = datetime.now(UTC)
    if exemption:
        breach.notification_exemption = exemption
    await session.flush()

    await _event(
        session, tenant_id=tenant_id, breach=breach, actor=actor,
        from_status=previous, to_status="closed",
        note=(
            "Closed."
            + (f" Notification exemption recorded: {exemption}" if exemption else "")
        ),
    )
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor, action=AuditAction.BREACH_CLOSED,
        entity_type="breach", entity_id=breach.id,
        payload={
            "reference": breach.reference,
            "affected": progress.total,
            "notified": progress.notified,
            "unnotified": progress.remaining,
            "exemption": exemption or None,
        },
    )
    return breach


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

async def list_for_tenant(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    status: str | None = None,
    severity: str | None = None,
    open_only: bool = False,
    limit: int = 100,
) -> list[Breach]:
    stmt = select(Breach).where(Breach.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(Breach.status == status)
    if severity:
        stmt = stmt.where(Breach.severity == severity)
    if open_only:
        stmt = stmt.where(Breach.status.notin_(("closed", "void")))
    rows = await session.execute(
        # Un-notified and oldest-discovered first: the order the statutory risk
        # arrives in, not the order things were typed up.
        stmt.order_by(
            Breach.board_notified_at.is_(None).desc(),
            Breach.discovered_at.asc().nullslast(),
        ).limit(limit)
    )
    return list(rows.scalars().all())


async def timeline(
    session: AsyncSession, tenant_id: uuid.UUID, breach_id: uuid.UUID
) -> list[BreachEvent]:
    rows = await session.execute(
        select(BreachEvent)
        .where(
            BreachEvent.tenant_id == tenant_id, BreachEvent.breach_id == breach_id
        )
        .order_by(BreachEvent.created_at)
    )
    return list(rows.scalars().all())


async def affected_list(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    breach_id: uuid.UUID,
    *,
    limit: int = 200,
    offset: int = 0,
) -> list[tuple[BreachAffectedPrincipal, DataPrincipal | None]]:
    """Who is on the list, for review before anything is sent.

    The most sensitive read in the product — who was affected by what — which is
    why nothing narrower than `breach:manage` reaches it.
    """
    rows = await session.execute(
        select(BreachAffectedPrincipal, DataPrincipal)
        .outerjoin(DataPrincipal, DataPrincipal.id == BreachAffectedPrincipal.principal_id)
        .where(
            BreachAffectedPrincipal.tenant_id == tenant_id,
            BreachAffectedPrincipal.breach_id == breach_id,
        )
        .order_by(BreachAffectedPrincipal.created_at)
        .offset(offset)
        .limit(limit)
    )
    return [(row[0], row[1]) for row in rows.all()]


async def counts(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, int]:
    """Register headline numbers.

    `board_overdue` is computed against the clock in the query rather than read
    from a column, for the same reason every other deadline in this product is: a
    stored flag is only as fresh as the last job that ran.
    """
    from datetime import timedelta

    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=BOARD_NOTIFICATION_HOURS)
    base = select(func.count()).select_from(Breach).where(Breach.tenant_id == tenant_id)

    return {
        "total": (await session.scalar(base)) or 0,
        "open": (
            await session.scalar(base.where(Breach.status.notin_(("closed", "void"))))
        ) or 0,
        "critical_open": (
            await session.scalar(
                base.where(
                    Breach.status.notin_(("closed", "void")),
                    Breach.severity.in_(("high", "critical")),
                )
            )
        ) or 0,
        "board_overdue": (
            await session.scalar(
                base.where(
                    Breach.status.notin_(("closed", "void")),
                    Breach.board_notified_at.is_(None),
                    Breach.discovered_at.isnot(None),
                    Breach.discovered_at < cutoff,
                )
            )
        ) or 0,
        "awaiting_principal_notice": (
            await session.scalar(
                base.where(
                    Breach.status.notin_(("closed", "void")),
                    Breach.principals_notified_at.is_(None),
                )
            )
        ) or 0,
    }
