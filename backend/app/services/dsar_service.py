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
from app.models.dsar import (
    CORRECTION_TYPES,
    DSAR_TYPES,
    DsarEvent,
    DsarRequest,
)
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


class DuplicateRequest(DsarRefused):
    """The same person already has this kind of request open.

    Its own class rather than a plain refusal, because the caller needs to be
    able to tell this apart: a duplicate is answered by pointing at the
    existing request, and every other refusal is not. Carries the reference so
    the message can name it.
    """

    def __init__(self, detail: str, **extra) -> None:
        super().__init__(detail, **extra)


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
    nomination_id: uuid.UUID | None = None,
    # Set by the public intake route. Carries two consequences: the request
    # is not dispatched until confirmed, and the confirmation email is sent
    # instead of the ordinary acknowledgement.
    arrived_publicly: bool = False,
    verification_token_hash: str | None = None,
    #: The link the confirmation email carries. Built by the caller from
    #: `public_base_url` — never from the incoming request's own host, which
    #: is how invitation links ended up pointing at an internal FQDN.
    public_confirm_url: str | None = None,
    # Staff override, for the case where somebody genuinely does need a
    # second request of the same kind open — a correction to a different
    # field while the first is still being made, most plausibly. Never
    # settable by a data principal raising their own request.
    allow_duplicate: bool = False,
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

    if type in CORRECTION_TYPES and not correction_payload:
        # The §12(1) family all need to say what should change; they differ in
        # what KIND of change it is, which is why they are separate types.
        what = {
            "correction": "what is wrong and what it should be",
            "completion": "what is missing and what should be added",
            "updating": "what has changed and what the new value is",
        }[type]
        raise DsarRefused(f"A {type} request has to say {what}.")

    # ------------------------------------------------------------ duplicates --
    #
    # Refused rather than silently merged. A person with an access request
    # already in progress who raises another has usually either forgotten or
    # not seen an acknowledgement, and the useful response is to point them at
    # the one that exists — not to open a second clock against the same work,
    # and not to quietly discard what they just asked for.
    #
    # Scoped to OPEN requests: asking again a year later is legitimate, and the
    # partial index this reads matches.
    if not allow_duplicate:
        open_same = await session.scalar(
            select(DsarRequest).where(
                DsarRequest.principal_id == principal_id,
                DsarRequest.type == type,
                DsarRequest.status.in_(("received", "verifying", "in_progress")),
            )
        )
        if open_same is not None:
            raise DuplicateRequest(
                f"You already have a {type} request in progress "
                f"({open_same.reference}), raised on "
                f"{open_same.submitted_at:%d %B %Y} and due by "
                f"{open_same.deadline_at:%d %B %Y}. We are working on it — "
                "there is no need to ask again, and raising a second one does "
                "not make the first any faster.",
                reference=open_same.reference,
                existing_request_id=str(open_same.id),
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
        nomination_id=nomination_id,
        arrived_publicly=arrived_publicly,
        verification_token_hash=verification_token_hash,
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

    # A publicly-raised request gets the CONFIRMATION email instead of the
    # acknowledgement. Sending both would bury the one action the person has to
    # take inside a message that reads as "nothing needed from you" — the same
    # reasoning `grievance.confirm` already follows.
    if arrived_publicly and public_confirm_url:
        key = "dsar.confirm"
        context = {
            "reference": request.reference,
            "type": type,
            "confirm_url": public_confirm_url,
            "deadline": deadline.date().isoformat(),
        }
    else:
        key = "dsar.received"
        context = {
            "reference": request.reference,
            "type": type,
            "deadline": deadline.date().isoformat(),
        }

    await notification_service.send_now(
        session,
        notification=await notification_service.enqueue(
            session,
            tenant_id=tenant_id,
            key=key,
            to_address=principal.email,
            context=context,
            entity_type="dsar_request",
            entity_id=request.id,
            principal_id=principal_id,
        ),
    )
    return request


# --------------------------------------------------------------------------- #
# Public intake: confirming the address
# --------------------------------------------------------------------------- #

def public_token() -> tuple[str, str]:
    """A confirmation token and its keyed hash.

    Keyed with the JWT secret rather than a bare SHA-256, so a stolen database
    does not yield a brute-forceable set of live confirmation links. Same
    discipline as invitations and password resets.
    """
    import hashlib
    import hmac
    import secrets

    secret = secrets.token_urlsafe(32)
    digest = hmac.new(
        _settings.jwt_secret.encode(), secret.encode(), hashlib.sha256
    ).hexdigest()
    return secret, digest


def _public_token_hash(secret: str) -> str:
    import hashlib
    import hmac

    return hmac.new(
        _settings.jwt_secret.encode(), (secret or "").encode(), hashlib.sha256
    ).hexdigest()


#: One message for every way confirmation can fail — wrong, spent, or never
#: existed. A caller must not be able to tell which, because each distinction is
#: a fact about somebody else's rights request.
PUBLIC_CONFIRM_GENERIC = (
    "That confirmation link is not valid. It may have expired, already been "
    "used, or been replaced by a newer request. Raise the request again."
)


async def confirm_public(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    reference: str,
    token: str,
) -> DsarRequest:
    """Redeem a confirmation token, which is what lets the request execute.

    Both the reference AND the token must match. The reference alone is
    guessable — DSAR-2026-0007 — and the token alone would be enough if the
    index were not unique, so requiring both means a guessed reference is
    useless without the emailed secret.

    Marks the request verified and spends the token. The CALLER dispatches to
    the engine afterwards, deliberately: `dispatch_to_engine` refuses an
    unconfirmed public request, so the ordering here is the safety property, and
    keeping the two separate makes it visible at the call site.
    """
    request = await session.scalar(
        select(DsarRequest).where(
            DsarRequest.reference == reference.strip().upper(),
            DsarRequest.verification_token_hash.is_not(None),
        )
    )
    if request is None:
        raise DsarRefused(PUBLIC_CONFIRM_GENERIC)

    import hmac as _hmac

    if not _hmac.compare_digest(
        request.verification_token_hash or "", _public_token_hash(token)
    ):
        raise DsarRefused(PUBLIC_CONFIRM_GENERIC)

    if not request.is_open:
        raise DsarRefused(
            f"{request.reference} is already {request.status}, so there is "
            "nothing left to confirm."
        )

    now = datetime.now(UTC)
    request.verified_at = now
    request.verification_method = "email"
    # Spent. Leaving the hash in place would keep a redeemable credential on a
    # row that no longer needs one.
    request.verification_token_hash = None

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.DSAR_STATUS_CHANGED,
        entity_type="dsar_request", entity_id=request.id,
        payload={
            "reference": request.reference,
            "confirmed": True,
            "method": "email",
            # No token, not even hashed. An audit trail an auditor reads is not
            # a place to put a credential's index.
            "arrived_publicly": True,
        },
    )
    await _event(
        session, tenant_id=tenant_id, request=request, actor=actor,
        note="Email address confirmed by the requester; the request may now "
             "be executed",
    )
    return request


async def dispatch_to_engine(
    session: AsyncSession, *, tenant_id: uuid.UUID, actor: Actor, request: DsarRequest
) -> DsarRequest:
    """Hand a request to whatever can actually carry it out.

    Access and erasure go to the Fides gateway. Correction does not — the
    engine has no correction action — and used to stop here, which left §12(1)
    as a tracked manual workflow: the product knew what was asked, tracked the
    deadline, and did nothing about the data.

    It now goes to `data_map_service.auto_correct`, which writes directly to the
    connected systems. That is a narrower path than it sounds: it applies itself
    only when the current value the person stated matches exactly one column in
    exactly one system, and otherwise leaves a plan for a human. See that
    function for why the person's own account of the data is what makes this
    verification rather than guesswork.

    The unconfirmed-public-request guard below covers BOTH, and has to: a
    correction dispatched on an unverified address writes a stranger's chosen
    value into somebody's record, which is worse than disclosing it.
    """
    # A request that arrived through the unauthenticated public form is a claim
    # about an email address until somebody proves they control the mailbox.
    # Dispatching one would disclose, delete or rewrite a person's data on the
    # strength of an address anybody could type into a form on a customer's
    # website.
    #
    # Checked HERE rather than only at the call site, because this is the single
    # function that makes data move — and a guard placed anywhere else can be
    # bypassed by the next caller who forgets it.
    if request.arrived_publicly and request.verified_at is None:
        logger.info(
            "not dispatching %s: raised publicly and not yet confirmed",
            request.reference,
        )
        return request

    # §12(1). Not the engine's to do, so it is done here against the connected
    # systems directly.
    if request.type in CORRECTION_TYPES:
        from app.services import data_map_service

        try:
            outcome = await data_map_service.auto_correct(
                session, tenant_id=tenant_id, actor=actor, request_id=request.id
            )
        except Exception as exc:  # noqa: BLE001
            # Same treatment as a gateway failure: the request is not lost, the
            # reason is on its timeline, and a human can carry it out by hand.
            request.engine_error = f"{type(exc).__name__}: {exc}"[:500]
            await session.flush()
            await _event(
                session, tenant_id=tenant_id, request=request, actor=actor,
                note=f"Automatic correction could not run: {exc}"[:500],
                automated=True,
            )
            return request

        if outcome.get("applied") is None:
            # A real answer, not a failure. Recorded so the DPO opening this
            # sees WHY it is waiting for them rather than an empty timeline.
            await _event(
                session, tenant_id=tenant_id, request=request, actor=actor,
                note=(
                    "Correction not applied automatically: "
                    f"{outcome.get('why_not_automatic')}. "
                    f"{len(outcome.get('confirmed') or [])} confirmed and "
                    f"{len(outcome.get('candidates') or [])} possible target(s) "
                    "are listed for review."
                )[:500],
                automated=True,
            )
        return request

    # Everything the engine can do. Kept as an explicit check after the
    # correction branch so a future sixth request type falls through to nothing
    # rather than being handed to the gateway under someone else's action name.
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

