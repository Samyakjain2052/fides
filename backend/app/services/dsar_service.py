"""The DSAR lifecycle: raise, execute, triage, resolve.

The engine is not reimplemented here. It already works — one privacy request
fans out across four datastores and masks identifiers on erasure. What this
module owns is the *record* of the request and the human workflow around it.

Two things it is careful about:

* **The statutory deadline is ours to compute**, from the tenant's SLA. A
  deadline supplied by a caller is not a deadline.
* **The engine must never overwrite a human decision.** If a DPO rejected a
  request, a late engine callback saying "complete" must not resurrect it. That
  is a one-line guard and the reason it exists is worth more than the line.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import Conflict, NotFound
from app.models.audit import AuditAction
from app.models.consent import DataPrincipal
from app.models.dsar import DSAR_TYPES, DsarEvent, DsarRequest
from app.models.tenant import Tenant
from app.services import audit_service
from app.services.audit_service import Actor

logger = logging.getLogger("app.dsar")
_settings = get_settings()

# How long an access package stays downloadable. It is one person's complete
# personal data in a single file; "forever" is not a defensible answer, and
# nothing here should encourage treating it as a permanent artifact.
PACKAGE_TTL = timedelta(days=7)

# Which status transitions a human may make. Written down rather than implied,
# because "which of these can I do next" is exactly the question a triage UI
# needs answered, and an undocumented state machine grows contradictions.
ALLOWED_TRANSITIONS = {
    "received": {"verifying", "in_progress", "rejected", "cancelled"},
    "verifying": {"in_progress", "rejected", "cancelled"},
    "in_progress": {"completed", "rejected", "cancelled"},
    "completed": set(),
    "rejected": set(),
    "cancelled": set(),
}

_ENGINE_ACTION = {"access": "access", "erasure": "erasure"}


class DsarRefused(Conflict):
    """A lawful or procedural reason the request cannot proceed as asked."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

async def _next_reference(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    """DSAR-2026-0007, per tenant, per year.

    Counting rather than a sequence: a per-tenant sequence would leak volume
    across tenants if it were global, and a human-quotable reference is worth
    more here than the tiny race a count admits — the UNIQUE constraint catches
    that, and a retry costs nothing.
    """
    year = datetime.now(UTC).year
    prefix = f"DSAR-{year}-"
    used = (
        await session.scalar(
            select(func.count())
            .select_from(DsarRequest)
            .where(
                DsarRequest.tenant_id == tenant_id,
                DsarRequest.reference.startswith(prefix),
            )
        )
    ) or 0
    return f"{prefix}{used + 1:04d}"


async def _event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request: DsarRequest,
    actor: Actor,
    to_status: str | None = None,
    from_status: str | None = None,
    note: str | None = None,
    automated: bool = False,
) -> None:
    session.add(
        DsarEvent(
            tenant_id=tenant_id,
            dsar_request_id=request.id,
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
# Raising a request
# --------------------------------------------------------------------------- #

async def submit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    principal_id: uuid.UUID,
    type: str,
    verification_method: str | None = None,
    verified: bool = False,
    correction_payload: dict[str, Any] | None = None,
    requested_by_actor: str = "principal",
) -> DsarRequest:
    """Record the request, then ask the engine to execute it.

    In that order, deliberately. If the engine call fails the request still
    exists at `received` with the failure on its timeline — losing somebody's
    rights request because a downstream was briefly unavailable would be the
    worst possible way to fail.
    """
    if type not in DSAR_TYPES:
        raise DsarRefused(f"Unknown request type {type!r}.")

    principal = await session.scalar(
        select(DataPrincipal).where(
            DataPrincipal.id == principal_id, DataPrincipal.tenant_id == tenant_id
        )
    )
    if principal is None:
        raise NotFound("No such data principal.")

    if type == "correction" and not correction_payload:
        raise DsarRefused(
            "A correction request has to say what is wrong and what it should be."
        )
    if type in ("access", "erasure") and not principal.email:
        # The engine locates a person by email — it is the identity every dataset
        # is annotated with. Without one there is nothing to execute against, and
        # saying so now beats a request that sits at `received` forever.
        raise DsarRefused(
            "This principal has no email on record, so an automated request "
            "cannot be executed against the connected systems."
        )

    tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
    now = datetime.now(UTC)
    deadline = now + timedelta(days=tenant.dsar_sla_days if tenant else 30)

    request = DsarRequest(
        tenant_id=tenant_id,
        principal_id=principal_id,
        reference=await _next_reference(session, tenant_id),
        type=type,
        status="received",
        submitted_at=now,
        deadline_at=deadline,
        verification_method=verification_method,
        verified_at=now if verified else None,
        requested_by_actor=requested_by_actor,
        correction_payload=correction_payload,
    )
    session.add(request)
    await session.flush()

    await _event(
        session, tenant_id=tenant_id, request=request, actor=actor,
        to_status="received",
        note=f"{type} request raised by {requested_by_actor}",
    )
    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action=AuditAction.DSAR_SUBMITTED,
        entity_type="dsar_request",
        entity_id=request.id,
        payload={
            "reference": request.reference,
            "type": type,
            "principal_id": str(principal_id),
            "deadline_at": deadline.isoformat(),
            "requested_by_actor": requested_by_actor,
            "verification_method": verification_method,
        },
    )
    # Tell the person. A suppression (no address, no template) is recorded on the
    # notification row rather than failing the request that has already happened.
    from app.services import notification_service

    await notification_service.send_now(
        session,
        notification=await notification_service.enqueue(
            session,
            tenant_id=tenant_id,
            key="dsar.received",
            to_address=principal.email,
            context={
                "reference": request.reference,
                "type": type,
                "deadline": deadline.date().isoformat(),
            },
            entity_type="dsar_request",
            entity_id=request.id,
            principal_id=principal_id,
        ),
    )
    return request


async def dispatch_to_engine(
    session: AsyncSession, *, tenant_id: uuid.UUID, actor: Actor, request: DsarRequest
) -> DsarRequest:
    """Hand an access/erasure request to the Fides gateway.

    Correction never reaches here — the engine has no correction action, so it
    stays a tracked manual workflow rather than being quietly dropped.
    """
    if request.type not in _ENGINE_ACTION:
        return request

    principal = await session.scalar(
        select(DataPrincipal).where(DataPrincipal.id == request.principal_id)
    )

    try:
        async with httpx.AsyncClient(timeout=_settings.gateway_timeout_seconds) as client:
            resp = await client.post(
                f"{_settings.gateway_url.rstrip('/')}/dsar",
                json={"email": principal.email, "action": _ENGINE_ACTION[request.type]},
            )
            resp.raise_for_status()
            created = resp.json()
    except Exception as exc:  # noqa: BLE001 — any transport failure is the same story
        # The request is NOT lost. It stays at `received` with the reason on its
        # timeline, and can be retried.
        request.engine_error = f"{type(exc).__name__}: {exc}"[:500]
        await session.flush()
        await _event(
            session, tenant_id=tenant_id, request=request, actor=actor,
            note=f"Engine dispatch failed: {request.engine_error}", automated=True,
        )
        logger.warning(
            "dsar engine dispatch failed",
            extra={"context": {"reference": request.reference, "error": str(exc)}},
        )
        return request

    previous = request.status
    request.engine_ref = created.get("request_id")
    request.engine_status = created.get("status")
    request.engine_error = None
    request.status = "in_progress"
    await session.flush()

    await _event(
        session, tenant_id=tenant_id, request=request, actor=actor,
        from_status=previous, to_status="in_progress",
        note=f"Dispatched to the engine as {request.engine_ref}", automated=True,
    )
    return request


# --------------------------------------------------------------------------- #
# Reconciling with the engine
# --------------------------------------------------------------------------- #

async def refresh_from_engine(
    session: AsyncSession, *, tenant_id: uuid.UUID, request: DsarRequest
) -> DsarRequest:
    """Poll the engine and reflect its status — without ever overruling a human.

    A DPO's rejection is a decision. A late callback from the engine saying
    "complete" must not undo it, and this guard is the only thing standing
    between that and a rejected request quietly reopening itself.
    """
    if not request.engine_ref or not request.is_open:
        return request

    try:
        async with httpx.AsyncClient(timeout=_settings.gateway_timeout_seconds) as client:
            resp = await client.get(
                f"{_settings.gateway_url.rstrip('/')}/dsar/{request.engine_ref}"
            )
            resp.raise_for_status()
            live = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "dsar engine poll failed",
            extra={"context": {"reference": request.reference, "error": str(exc)}},
        )
        return request

    engine_status = live.get("status")
    request.engine_status = engine_status

    if engine_status == "complete" and request.status == "in_progress":
        request.status = "completed"
        request.resolved_at = datetime.now(UTC)
        if request.type == "access":
            request.package_available_until = datetime.now(UTC) + PACKAGE_TTL
    elif engine_status == "error" and request.status == "in_progress":
        # Not auto-rejected. An engine failure is an operational problem for a
        # human to look at, not a decision about the person's rights.
        request.engine_error = "The engine reported an error executing this request."

    await session.flush()
    return request


# --------------------------------------------------------------------------- #
# Triage
# --------------------------------------------------------------------------- #

async def change_status(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    request: DsarRequest,
    to_status: str,
    reason: str | None = None,
    note: str | None = None,
) -> DsarRequest:
    allowed = ALLOWED_TRANSITIONS.get(request.status, set())
    if to_status not in allowed:
        raise DsarRefused(
            f"A {request.status} request cannot become {to_status}."
            + (f" Allowed: {', '.join(sorted(allowed))}." if allowed else
               " It is already closed.")
        )

    if to_status == "rejected" and not (reason or "").strip():
        # The database enforces this too. Both, because a rejection with no
        # recorded reason is indefensible and this is the friendlier of the two
        # places to find that out.
        raise DsarRefused("A rejection has to say why.")

    previous = request.status
    request.status = to_status
    if to_status == "rejected":
        request.rejection_reason = reason.strip()
        request.resolved_at = datetime.now(UTC)
    elif to_status in ("completed", "cancelled"):
        request.resolved_at = datetime.now(UTC)
        if to_status == "completed" and request.type == "access":
            request.package_available_until = datetime.now(UTC) + PACKAGE_TTL
    await session.flush()

    await _event(
        session, tenant_id=tenant_id, request=request, actor=actor,
        from_status=previous, to_status=to_status, note=note or reason,
    )
    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action=(
            AuditAction.DSAR_COMPLETED
            if to_status == "completed"
            else AuditAction.DSAR_STATUS_CHANGED
        ),
        entity_type="dsar_request",
        entity_id=request.id,
        payload={
            "reference": request.reference,
            "from": previous,
            "to": to_status,
            "reason": reason,
            "note": note,
        },
    )

    # Only the outcomes a person needs to hear about. Notifying on every internal
    # transition would train people to ignore these, which is worse than not
    # sending them.
    if to_status in ("completed", "rejected"):
        from app.services import notification_service

        principal = await session.scalar(
            select(DataPrincipal).where(DataPrincipal.id == request.principal_id)
        )
        await notification_service.send_now(
            session,
            notification=await notification_service.enqueue(
                session,
                tenant_id=tenant_id,
                key=f"dsar.{to_status}",
                to_address=principal.email if principal else None,
                context={
                    "reference": request.reference,
                    "type": request.type,
                    "reason": reason or "",
                },
                entity_type="dsar_request",
                entity_id=request.id,
                principal_id=request.principal_id,
            ),
        )
    return request


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

async def get(session: AsyncSession, tenant_id: uuid.UUID, request_id: uuid.UUID) -> DsarRequest:
    row = await session.scalar(
        select(DsarRequest).where(
            DsarRequest.id == request_id, DsarRequest.tenant_id == tenant_id
        )
    )
    if row is None:
        raise NotFound("No such request.")
    return row


async def timeline(
    session: AsyncSession, tenant_id: uuid.UUID, request_id: uuid.UUID
) -> list[DsarEvent]:
    rows = await session.execute(
        select(DsarEvent)
        .where(DsarEvent.dsar_request_id == request_id)
        .order_by(DsarEvent.created_at)
    )
    return list(rows.scalars().all())


async def package(
    session: AsyncSession, *, tenant_id: uuid.UUID, actor: Actor, request: DsarRequest
) -> dict[str, Any]:
    """Fetch the access package, and record that somebody did.

    This is the most sensitive object the product hands out: one person's
    complete personal data in a single response. Every retrieval is audited, and
    an expired package says "expired" rather than 404 — the person is entitled to
    know it existed and that the window closed, not to be told it never was.
    """
    if request.type != "access":
        raise DsarRefused("Only an access request produces a package.")
    if not request.engine_ref:
        raise DsarRefused("This request never reached the engine.")
    if request.status != "completed":
        raise DsarRefused("The package is not ready yet.")
    if (
        request.package_available_until
        and request.package_available_until <= datetime.now(UTC)
    ):
        raise DsarRefused(
            "This access package has expired. Packages are available for "
            f"{PACKAGE_TTL.days} days; raise a new request to receive another."
        )

    async with httpx.AsyncClient(timeout=_settings.gateway_timeout_seconds) as client:
        resp = await client.get(
            f"{_settings.gateway_url.rstrip('/')}/dsar/{request.engine_ref}"
        )
        resp.raise_for_status()
        live = resp.json()

    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action=AuditAction.DSAR_COMPLETED,
        entity_type="dsar_package",
        entity_id=request.id,
        payload={
            "reference": request.reference,
            "downloaded": True,
            "collections": list((live.get("data") or {}).keys()),
        },
    )
    await _event(
        session, tenant_id=tenant_id, request=request, actor=actor,
        note="Access package retrieved",
    )
    return live
