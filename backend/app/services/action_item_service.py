"""Fanning a request out into per-system work, and closing each piece honestly.

`data_map_service` sweeps every connection and reports what it found. This turns
that sweep into work somebody owns, because the sweep alone cannot answer the
two questions an audit actually asks: who was responsible for this system, and
what did they conclude?

THE RULE THIS MODULE EXISTS TO ENFORCE

An item closes with an outcome and an attestation, or it does not close. A
"completed" row with no stated conclusion is the exact silence the table exists
to prevent — and the database refuses it, so no future caller can produce one by
forgetting.

That includes the negative case, which is the one products get wrong. "We
searched payroll and it holds nothing about this person" is a finding. Closing
an item with `no_records_matched` records it as one, with a name and a
timestamp against it. A scan that simply returns no rows expresses that and
"nobody looked at payroll" identically.

FAN-OUT IS IDEMPOTENT

A unique constraint on (request, connection) plus a savepoint per insert, so
re-running it after a new connection is added creates only the missing items and
never disturbs work somebody has already claimed. Re-running a fan-out is a
normal thing to do — connections get added mid-request — and it must not be
destructive.

WHAT IT DELIBERATELY DOES NOT DO

It does not close the request. Every item being done is strong evidence that the
obligation is discharged and it is not the same statement: whether everything in
scope was reached is a judgement a human makes and signs. The same line
`data_map_service.erase` already refuses to cross.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.connectors import registry
from app.core.errors import Conflict, NotFound, ValidationProblem
from app.models.audit import AuditAction
from app.models.connection import Connection
from app.models.dsar import DsarRequest
from app.models.dsar_action_item import DsarActionItem
from app.models.user import User
from app.services import audit_service
from app.services.audit_service import Actor
from app.services.dsar_service import _event

logger = logging.getLogger("app.action_items")


class ActionItemRefused(Conflict):
    """A procedural reason this item cannot be closed as asked."""


async def fan_out(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    request: DsarRequest,
) -> list[DsarActionItem]:
    """Create one item per connected system. Safe to run repeatedly.

    Only `connected` connections get an item, and that is a deliberate
    narrowing rather than an oversight: an item against a system whose
    credentials have never worked would be work nobody can do, and it would make
    a queue look busy while the actual problem is on the Connections screen.
    A failing connection is a connections problem, and it already alerts.

    Each insert gets its own savepoint. Without one, a duplicate-key error from
    a concurrent fan-out would poison the caller's whole transaction — the trap
    that produced the 500 on duplicate retention policy names.
    """
    rows = (
        await session.execute(
            select(Connection)
            .where(Connection.status == "connected")
            .order_by(Connection.connector_id, Connection.label)
        )
    ).scalars().all()

    created: list[DsarActionItem] = []
    for connection in rows:
        connector = registry.get(connection.connector_id)
        # An item is automated when the connector can genuinely act on its own.
        # `live` connectors can discover and mask; everything else needs a
        # person, and pretending otherwise would leave items sitting in a state
        # that nothing is coming to finish.
        automated = bool(connector and connector.status == "live")

        item = DsarActionItem(
            tenant_id=tenant_id,
            dsar_request_id=request.id,
            connection_id=connection.id,
            system_label=f"{connector.label if connector else connection.connector_id}"
                         f" · {connection.label}",
            assignee_user_id=connection.owner_user_id,
            automated=automated,
        )
        savepoint = await session.begin_nested()
        session.add(item)
        try:
            await savepoint.commit()
        except IntegrityError:
            # Already exists. Someone else's fan-out got here first, or this is
            # a re-run; either way the existing item is the one that matters.
            await savepoint.rollback()
            continue
        created.append(item)

    if created:
        await audit_service.record(
            session, tenant_id=tenant_id, actor=actor,
            action=AuditAction.DSAR_ACTION_ITEMS_CREATED,
            entity_type="dsar_request", entity_id=request.id,
            payload={
                "reference": request.reference,
                "created": len(created),
                "systems": [i.system_label for i in created][:50],
                "unassigned": sum(
                    1 for i in created
                    if i.assignee_user_id is None and not i.automated
                ),
            },
        )
        await _event(
            session, tenant_id=tenant_id, request=request, actor=actor,
            note=(
                f"Fanned out to {len(created)} system(s). "
                + (
                    f"{sum(1 for i in created if i.assignee_user_id is None and not i.automated)} "
                    "need an owner."
                    if any(
                        i.assignee_user_id is None and not i.automated
                        for i in created
                    )
                    else "All assigned."
                )
            ),
            automated=True,
        )
    return created


async def add_manual(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    request: DsarRequest,
    system_label: str,
    assignee_user_id: uuid.UUID | None = None,
) -> DsarActionItem:
    """An item for a system this product cannot reach.

    A processor's own database, an offline archive, a payroll bureau. These are
    the majority of systems in most organisations, and a rights-request tool
    that can only track what it has credentials for is tracking the easy part.
    """
    label = (system_label or "").strip()
    if not label:
        raise ValidationProblem("Name the system this item is about.")

    item = DsarActionItem(
        tenant_id=tenant_id,
        dsar_request_id=request.id,
        connection_id=None,
        system_label=label[:160],
        assignee_user_id=assignee_user_id,
        automated=False,
    )
    session.add(item)
    await session.flush()

    await _event(
        session, tenant_id=tenant_id, request=request, actor=actor,
        note=f"Added a manual item for {label}",
    )
    return item


async def get(session, *, item_id: uuid.UUID) -> DsarActionItem:
    item = await session.scalar(
        select(DsarActionItem).where(DsarActionItem.id == item_id)
    )
    if item is None:
        raise NotFound("No such action item.")
    return item


async def for_request(session, *, request_id: uuid.UUID) -> list[DsarActionItem]:
    rows = await session.execute(
        select(DsarActionItem)
        .where(DsarActionItem.dsar_request_id == request_id)
        .order_by(DsarActionItem.system_label)
    )
    return list(rows.scalars().all())


async def assign(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    item: DsarActionItem,
    assignee_user_id: uuid.UUID | None,
) -> DsarActionItem:
    """Give an item to somebody, or take it back to unassigned.

    Assigning to nobody is allowed and is not the same as leaving it alone: it
    is how a DPO hands an item back when the wrong person has it.
    """
    if item.status in ("completed", "skipped"):
        raise ActionItemRefused(
            f"{item.system_label} is already {item.status}. Reopen it before "
            "reassigning."
        )

    if assignee_user_id is not None:
        target = await session.scalar(
            select(User).where(User.id == assignee_user_id, User.is_active)
        )
        if target is None:
            # Covers both "no such user" and "revoked" — and RLS means a user
            # from another workspace is simply not there.
            raise ActionItemRefused(
                "That person does not have an active account in this workspace."
            )

    item.assignee_user_id = assignee_user_id
    if assignee_user_id is None:
        item.status = "pending"
        item.claimed_at = None

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.DSAR_ACTION_ITEM_ASSIGNED,
        entity_type="dsar_action_item", entity_id=item.id,
        payload={
            "system": item.system_label,
            "assignee": str(assignee_user_id) if assignee_user_id else None,
        },
    )
    return item


async def claim(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    item: DsarActionItem,
    user_id: uuid.UUID,
) -> DsarActionItem:
    """Say you are doing it. Also assigns it, since claiming implies that."""
    if item.status in ("completed", "skipped"):
        raise ActionItemRefused(f"{item.system_label} is already {item.status}.")

    item.assignee_user_id = user_id
    item.status = "claimed"
    item.claimed_at = datetime.now(UTC)
    return item


async def close(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    request: DsarRequest,
    item: DsarActionItem,
    user_id: uuid.UUID,
    outcome: str,
    attestation: str,
    records_found: int = 0,
    basis: str | None = None,
    internal_notes: str | None = None,
) -> DsarActionItem:
    """Close an item with a stated conclusion.

    `attestation` is required and is not decoration. It is the sentence somebody
    was willing to write about what they found, and it is the thing an auditor
    reads. A dropdown value on its own — `no_records_matched` — records that a
    button was pressed; a sentence records that a person looked.

    `basis` is required for `retained`, because retaining somebody's data
    against their erasure request is a legal claim and needs a stated ground.
    """
    if item.status in ("completed", "skipped"):
        raise ActionItemRefused(
            f"{item.system_label} is already {item.status}. Reopen it first if "
            "the conclusion has changed."
        )
    if outcome not in (
        "data_found", "no_records_matched", "erased", "retained",
        "third_party_asked",
    ):
        raise ValidationProblem(f"Unknown outcome {outcome!r}.")

    statement = (attestation or "").strip()
    if not statement:
        raise ValidationProblem(
            "Say what you found, in a sentence. A dropdown value records that a "
            "button was pressed; this records that somebody looked."
        )

    if outcome == "retained" and not (basis or "").strip():
        raise ValidationProblem(
            "Retaining data against a request needs a stated lawful ground — "
            "which obligation requires you to keep it."
        )

    if outcome == "no_records_matched" and records_found:
        raise ValidationProblem(
            f"You reported {records_found} record(s) found and also that nothing "
            "matched. One of those is wrong."
        )

    now = datetime.now(UTC)
    item.status = "completed"
    item.outcome = outcome
    item.attestation = statement
    item.records_found = max(0, records_found)
    item.completed_at = now
    item.completed_by = user_id
    if basis:
        item.skip_reason = basis.strip()
    if internal_notes is not None:
        item.internal_notes = internal_notes.strip() or None

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.DSAR_ACTION_ITEM_CLOSED,
        entity_type="dsar_action_item", entity_id=item.id,
        payload={
            "reference": request.reference,
            "system": item.system_label,
            "outcome": outcome,
            "records_found": item.records_found,
            # The attestation IS the evidence, so it belongs in the chain. It is
            # a statement about handling, not a disclosed value.
            "attestation": statement[:1000],
            "basis": item.skip_reason,
        },
    )
    await _event(
        session, tenant_id=tenant_id, request=request, actor=actor,
        note=f"{item.system_label}: {outcome.replace('_', ' ')} — {statement}",
    )
    return item


async def skip(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    request: DsarRequest,
    item: DsarActionItem,
    reason: str,
) -> DsarActionItem:
    """Deliberately not do it, with a reason. Not the same as failing."""
    text = (reason or "").strip()
    if not text:
        raise ValidationProblem(
            "Skipping an item needs a reason. An item nobody did for no recorded "
            "reason is indistinguishable from one that was forgotten."
        )
    item.status = "skipped"
    item.skip_reason = text

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.DSAR_ACTION_ITEM_CLOSED,
        entity_type="dsar_action_item", entity_id=item.id,
        payload={
            "reference": request.reference,
            "system": item.system_label,
            "outcome": "skipped",
            "reason": text[:1000],
        },
    )
    await _event(
        session, tenant_id=tenant_id, request=request, actor=actor,
        note=f"{item.system_label}: skipped — {text}",
    )
    return item


async def mark_failed(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    item: DsarActionItem,
    reason: str,
) -> DsarActionItem:
    """Attempted and could not be finished. Stays open; a human is needed."""
    text = (reason or "").strip()
    if not text:
        raise ValidationProblem("Say what went wrong.")
    item.status = "failed"
    item.failure_reason = text[:2000]
    return item


async def reopen(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    item: DsarActionItem,
    reason: str,
) -> DsarActionItem:
    """Undo a close.

    Clears the conclusion rather than leaving a stale one attached to an open
    item — a `completed_at` on something in progress is how a report ends up
    counting the same work twice. The audit chain keeps the superseded
    attestation, so reopening does not erase what was previously claimed.
    """
    if item.status not in ("completed", "skipped"):
        raise ActionItemRefused(f"{item.system_label} is not closed.")
    text = (reason or "").strip()
    if not text:
        raise ValidationProblem("Reopening needs a reason.")

    previous = item.outcome or item.status
    item.status = "claimed" if item.assignee_user_id else "pending"
    item.outcome = None
    item.attestation = None
    item.completed_at = None
    item.completed_by = None
    item.skip_reason = None

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.DSAR_ACTION_ITEM_REOPENED,
        entity_type="dsar_action_item", entity_id=item.id,
        payload={
            "system": item.system_label,
            "was": previous,
            "reason": text[:1000],
        },
    )
    return item


async def record_third_party(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    item: DsarActionItem,
    address: str,
    note: str | None = None,
) -> DsarActionItem:
    """Record that somebody outside was told to act.

    §8(2) keeps the fiduciary responsible for its processors, so "we asked our
    vendor to delete it" is a fact worth being able to produce with a date on
    it. Appended rather than replaced: a system frequently has more than one
    party to tell, and each needs its own timestamp.
    """
    target = (address or "").strip()
    if not target:
        raise ValidationProblem("Who was told?")

    entry = {
        "address": target[:320],
        "notified_at": datetime.now(UTC).isoformat(),
        "note": (note or "").strip()[:500] or None,
    }
    # Reassigned rather than appended in place: SQLAlchemy does not track
    # mutation of a JSONB list, so `.append()` would not be persisted.
    item.third_parties_notified = [*item.third_parties_notified, entry]

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.DSAR_THIRD_PARTY_NOTIFIED,
        entity_type="dsar_action_item", entity_id=item.id,
        payload={"system": item.system_label, **entry},
    )
    return item


def as_dict(item: DsarActionItem, assignee: User | None = None) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "connection_id": str(item.connection_id) if item.connection_id else None,
        "system_label": item.system_label,
        "status": item.status,
        "outcome": item.outcome,
        "automated": item.automated,
        "records_found": item.records_found,
        "attestation": item.attestation,
        "skip_reason": item.skip_reason,
        "failure_reason": item.failure_reason,
        "internal_notes": item.internal_notes,
        "third_parties_notified": item.third_parties_notified,
        "assignee_user_id": (
            str(item.assignee_user_id) if item.assignee_user_id else None
        ),
        "assignee_label": (
            (assignee.full_name or assignee.email) if assignee else None
        ),
        "claimed_at": item.claimed_at,
        "completed_at": item.completed_at,
        "is_open": item.is_open,
    }


def summarise(items: list[DsarActionItem]) -> dict[str, Any]:
    """Counts a screen and a report both need.

    `all_closed` is offered as information and never acts on its own. Every item
    being done is strong evidence the obligation is discharged and is not the
    same statement — whether everything in scope was reached is a judgement a
    human makes and signs.
    """
    return {
        "total": len(items),
        "open": sum(1 for i in items if i.is_open),
        "completed": sum(1 for i in items if i.status == "completed"),
        "skipped": sum(1 for i in items if i.status == "skipped"),
        "failed": sum(1 for i in items if i.status == "failed"),
        "unassigned": sum(
            1 for i in items
            if i.assignee_user_id is None and not i.automated and i.is_open
        ),
        "records_found": sum(i.records_found for i in items),
        "nothing_found_in": sum(
            1 for i in items if i.outcome == "no_records_matched"
        ),
        "all_closed": bool(items) and all(not i.is_open for i in items),
    }
