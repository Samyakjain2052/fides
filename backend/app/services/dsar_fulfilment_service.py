"""Actually answering a rights request: identity, correspondence, delivery.

`dsar_service` owns the lifecycle — raise, dispatch, triage, resolve. This owns
the three things that turn a tracked request into a fulfilled one, and they are
in one module because they are one workflow:

  1. identity   a document is submitted, a human reviews it, and the decision
                is recorded with its reason
  2. the thread correspondence in both directions, attached to the request
  3. delivery   a package is assembled once, hashed, stored, and delivered into
                the thread

WHY DELIVERY GOES THROUGH THE THREAD

Because the alternative is email, and emailing somebody's complete personal
data is the single worst thing this product could do. Mail is unencrypted in
transit between arbitrary hops, sits in an inbox for years, and is forwarded by
accident. What goes out by email is a notification that the package is ready;
the package itself is fetched over TLS from an endpoint only its subject can
call, and it expires.

ASSEMBLED IS NOT DELIVERED

Two timestamps, deliberately. A package can be assembled, reviewed against the
redacted preview, and found wrong — and "we prepared it" must never be able to
read as "they received it" in an audit. The same distinction the breach module
already makes between recording and notifying.

WHAT AN ADMINISTRATOR CAN AND CANNOT SEE

They can see the preview: field names, locations, and the values that identify
neither a household nor an account. They cannot download the package — no staff
capability grants it. Reviewing a disclosure does not require reading somebody's
government ID, and a product where every DPO can is a product with a much larger
breach surface than it needs. See `disclosure` for the reasoning in full.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import Conflict, NotFound, ValidationProblem
from app.models.audit import AuditAction
from app.models.consent import DataPrincipal
from app.models.dsar import DsarRequest
from app.models.dsar_message import DsarMessage
from app.models.stored_file import StoredFile
from app.services import audit_service, disclosure, file_service, notification_service
from app.services.audit_service import Actor
from app.services.dsar_service import PACKAGE_TTL, DsarRefused, _event

logger = logging.getLogger("app.dsar.fulfilment")
_settings = get_settings()

#: Cap on messages per request. Not a rate limit — `throttle` does that — but a
#: ceiling on how large one request's correspondence can grow, because the whole
#: thread is loaded to render it and an unbounded list is a slow page waiting to
#: happen.
MAX_THREAD_MESSAGES = 500


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #

async def submit_identity_document(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    request: DsarRequest,
    filename: str,
    data: bytes,
    declared_content_type: str | None,
    principal_id: uuid.UUID | None = None,
    uploaded_by: uuid.UUID | None = None,
) -> StoredFile:
    """Attach a document proving who is asking.

    Replacing an earlier document purges the earlier one rather than keeping
    both. Somebody who uploaded the wrong photograph should not leave a copy of
    it with us forever, and "we still hold the passport they uploaded by
    mistake" is not a position worth defending.
    """
    if not request.is_open:
        raise DsarRefused(
            f"{request.reference} is {request.status}; identity documents "
            "cannot be added to a closed request."
        )

    previous = request.identity_document_id

    stored = await file_service.store(
        session,
        tenant_id=tenant_id,
        purpose="dsar_id_document",
        entity_type="dsar_request",
        entity_id=request.id,
        filename=filename,
        data=data,
        declared_content_type=declared_content_type,
        uploaded_by=uploaded_by,
        uploaded_by_principal=principal_id,
    )

    request.identity_document_id = stored.id
    # A new document reopens the decision. Leaving a prior acceptance in place
    # would let somebody swap the evidence after it was approved.
    request.identity_reviewed_at = None
    request.identity_reviewed_by = None
    request.identity_rejection_reason = None

    if previous is not None:
        await file_service.purge(
            session, file_id=previous, reason="replaced by a newer document"
        )

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.DSAR_IDENTITY_SUBMITTED,
        entity_type="dsar_request", entity_id=request.id,
        # The hash, not the document, and not its filename either — a filename
        # is frequently somebody's full name and passport number.
        payload={
            "reference": request.reference,
            "sha256": stored.sha256,
            "content_type": stored.content_type,
            "byte_size": stored.byte_size,
            "replaced_previous": previous is not None,
        },
    )
    await _event(
        session, tenant_id=tenant_id, request=request, actor=actor,
        note=(
            "Identity document submitted"
            + (" (replacing an earlier one)" if previous else "")
        ),
    )
    return stored


async def review_identity(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    request: DsarRequest,
    reviewer_id: uuid.UUID,
    accept: bool,
    reason: str | None = None,
) -> DsarRequest:
    """Record the decision a human made about the document.

    A refusal must carry a reason. This is the one refusal a person is most
    likely to challenge, and "we could not verify you" with nothing behind it is
    indistinguishable from not having looked.

    Accepting also purges the document. Its purpose is exhausted the moment
    identity is confirmed — §8(7) — and the alternative is a growing archive of
    photographed government IDs whose only remaining function is to be stolen.
    """
    if request.identity_document_id is None:
        raise DsarRefused(
            "There is no identity document on this request to review."
        )
    if not accept and not (reason or "").strip():
        raise ValidationProblem(
            "Refusing on identity grounds needs a reason. The person is "
            "entitled to know why, and may challenge it."
        )

    now = datetime.now(UTC)
    request.identity_reviewed_by = reviewer_id
    request.identity_reviewed_at = now

    if accept:
        request.identity_rejection_reason = None
        request.verified_at = now
        request.verification_method = request.verification_method or "document"
        # Purpose served. Destroy the bytes, keep the record that we held it.
        await file_service.purge(
            session,
            file_id=request.identity_document_id,
            reason="identity confirmed — purpose served",
        )
        note = "Identity verified from the submitted document, which was then destroyed"
        action = AuditAction.DSAR_IDENTITY_VERIFIED
    else:
        request.identity_rejection_reason = reason.strip()
        request.verified_at = None
        note = f"Identity NOT verified: {request.identity_rejection_reason}"
        action = AuditAction.DSAR_IDENTITY_REJECTED

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor, action=action,
        entity_type="dsar_request", entity_id=request.id,
        payload={
            "reference": request.reference,
            "reviewer": str(reviewer_id),
            "accepted": accept,
            "reason": request.identity_rejection_reason,
        },
    )
    await _event(
        session, tenant_id=tenant_id, request=request, actor=actor, note=note
    )
    return request


async def note_identity_viewed(
    session, *, tenant_id: uuid.UUID, actor: Actor, request: DsarRequest
) -> None:
    """Record that somebody opened the document.

    Looking at a photograph of somebody's government ID is itself processing,
    and the person whose ID it is has a right to know who looked. Called by the
    download route rather than the reviewer, so a look that leads to no decision
    is still recorded.
    """
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.DSAR_IDENTITY_VIEWED,
        entity_type="dsar_request", entity_id=request.id,
        payload={"reference": request.reference},
    )


# --------------------------------------------------------------------------- #
# The message thread
# --------------------------------------------------------------------------- #

async def post_message(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    request: DsarRequest,
    body: str,
    direction: str,
    author_user_id: uuid.UUID | None = None,
    author_principal_id: uuid.UUID | None = None,
    author_label: str,
    automated: bool = False,
    notify: bool = True,
) -> DsarMessage:
    """Add one message to the thread, and tell the other side.

    The notification carries no content. A message about a rights request will
    routinely quote personal data, and an email is not a safe place to put it —
    so what goes out is "there is a new message about DSAR-2026-0002", and
    reading it requires signing in.
    """
    text = (body or "").strip()
    if not text:
        raise ValidationProblem("A message needs some text.")
    if len(text) > 20_000:
        raise ValidationProblem(
            "That message is too long. Attach a document instead."
        )

    count = len(await thread(session, request_id=request.id))
    if count >= MAX_THREAD_MESSAGES:
        raise DsarRefused(
            f"This request already has {count} messages. Raise a new request "
            "rather than continuing this thread indefinitely."
        )

    message = DsarMessage(
        tenant_id=tenant_id,
        dsar_request_id=request.id,
        direction=direction,
        author_user_id=author_user_id,
        author_principal_id=author_principal_id,
        author_label=author_label[:255],
        body=text,
        automated=automated,
    )
    session.add(message)
    await session.flush()

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=(
            AuditAction.DSAR_MESSAGE_SENT
            if direction == "to_principal"
            else AuditAction.DSAR_MESSAGE_RECEIVED
        ),
        entity_type="dsar_request", entity_id=request.id,
        # Length and direction, never the body. The body is correspondence about
        # somebody's personal data and the audit trail is read by auditors.
        payload={
            "reference": request.reference,
            "direction": direction,
            "message_id": str(message.id),
            "characters": len(text),
            "automated": automated,
        },
    )

    if notify and direction == "to_principal":
        principal = await session.scalar(
            select(DataPrincipal).where(DataPrincipal.id == request.principal_id)
        )
        if principal is not None and principal.email:
            await notification_service.enqueue(
                session,
                tenant_id=tenant_id,
                key="dsar.message",
                to_address=principal.email,
                # Keyed to the MESSAGE, not the request. `enqueue` dedupes on
                # (tenant, template, entity), so keying this to the request
                # would send the first message and silently swallow every reply
                # after it — the same trap that ate the second password-reset
                # email and the repeat connection alerts.
                entity_type="dsar_message",
                entity_id=message.id,
                context={"reference": request.reference},
            )
            message.notified_at = datetime.now(UTC)

    await _event(
        session, tenant_id=tenant_id, request=request, actor=actor,
        note=(
            "Message sent to the requester"
            if direction == "to_principal"
            else "Message received from the requester"
        ),
        automated=automated,
    )
    return message


async def thread(session, *, request_id: uuid.UUID) -> list[DsarMessage]:
    """The whole conversation, oldest first."""
    rows = await session.execute(
        select(DsarMessage)
        .where(DsarMessage.dsar_request_id == request_id)
        .order_by(DsarMessage.created_at)
    )
    return list(rows.scalars().all())


async def mark_read(
    session, *, request_id: uuid.UUID, reader_is_staff: bool
) -> int:
    """Stamp the messages travelling the other way as seen.

    Staff reading marks `from_principal` messages; the principal reading marks
    `to_principal` ones. Marking your own messages read would make the receipt
    meaningless, which is a small thing until somebody asks whether the person
    ever saw the refusal.
    """
    unread_direction = "from_principal" if reader_is_staff else "to_principal"
    rows = (
        await session.execute(
            select(DsarMessage).where(
                DsarMessage.dsar_request_id == request_id,
                DsarMessage.direction == unread_direction,
                DsarMessage.read_at.is_(None),
            )
        )
    ).scalars().all()
    now = datetime.now(UTC)
    for row in rows:
        row.read_at = now
    return len(rows)


def message_as_dict(row: DsarMessage, attachments: list[StoredFile]) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "direction": row.direction,
        "author_label": row.author_label,
        "body": row.body,
        "automated": row.automated,
        "created_at": row.created_at,
        "read_at": row.read_at,
        "notified_at": row.notified_at,
        "attachments": [file_service.as_dict(a) for a in attachments],
    }


# --------------------------------------------------------------------------- #
# The package
# --------------------------------------------------------------------------- #

async def _engine_data(request: DsarRequest) -> dict[str, Any]:
    """Whatever the engine found, or nothing.

    A request that never reached the engine — a correction, or one handled
    entirely through the connections path — returns an empty mapping rather than
    raising. "The engine had nothing" and "there is nothing" are different, and
    the caller decides what to do about it; see `assemble`.
    """
    if not request.engine_ref:
        return {}
    try:
        async with httpx.AsyncClient(
            timeout=_settings.gateway_timeout_seconds
        ) as client:
            response = await client.get(
                f"{_settings.gateway_url.rstrip('/')}/dsar/{request.engine_ref}"
            )
            response.raise_for_status()
            return (response.json() or {}).get("data") or {}
    except Exception as exc:  # noqa: BLE001
        # Not fatal. A package assembled from the data map alone is still a
        # disclosure, and refusing to produce anything because one source is
        # unreachable serves nobody.
        logger.warning(
            "engine fetch failed while assembling %s: %s", request.reference, exc
        )
        return {}


async def preview(
    session, *, tenant_id: uuid.UUID, actor: Actor, request: DsarRequest
) -> dict[str, Any]:
    """What staff see before delivering. Sensitive values excluded.

    Audited, because building this reads the person's data even though it does
    not show all of it.
    """
    if request.type != "access":
        raise DsarRefused(
            f"{request.reference} is a {request.type} request. Only an access "
            "request produces a disclosure package."
        )

    data = await _engine_data(request)
    result = disclosure.preview(data)

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.DSAR_DATA_MAP_BUILT,
        entity_type="dsar_request", entity_id=request.id,
        payload={
            "reference": request.reference,
            "previewed": True,
            "fields": result["field_count"],
            "collections": result["collections"],
            "sensitive_excluded": result["counts"]["sensitive"],
            "withheld": result["counts"]["never"],
        },
    )
    return result


async def assemble(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    request: DsarRequest,
    confirm_reference: str,
) -> tuple[StoredFile, dict[str, Any]]:
    """Build the package, store it, hash it. Does not deliver it.

    Typing the reference back is the same guard the retention live run and the
    connected erasure already use. This one assembles one person's complete
    personal data into a single object, and that should not follow from a single
    unremarkable click.

    Re-assembling replaces the previous package and purges it, so there is never
    more than one live copy of somebody's whole record lying around.
    """
    if (confirm_reference or "").strip().upper() != request.reference.upper():
        raise DsarRefused(
            f"To assemble the package, type the request's reference "
            f"({request.reference}) to confirm. It gathers this person's entire "
            "record into one file."
        )
    if request.type != "access":
        raise DsarRefused(
            f"{request.reference} is a {request.type} request, not an access "
            "request. Assembling a disclosure package for it would be answering "
            "a question nobody asked."
        )
    if not request.is_open and request.status != "completed":
        raise DsarRefused(
            f"{request.reference} is {request.status}; there is nothing to "
            "disclose."
        )

    data = await _engine_data(request)
    summary = disclosure.preview(data)

    if not summary["field_count"]:
        # An empty disclosure is a legitimate answer — "we hold nothing about
        # you" — but it must be a deliberate one, because the same emptiness is
        # what an unreachable engine produces. Making the caller say so out loud
        # is the difference between a nil return and a silent failure.
        raise DsarRefused(
            "No personal data was found for this person, so there is nothing to "
            "package. If that is the correct answer, reply to the requester "
            "saying so and complete the request — do not send an empty file. "
            "If it is not, check that the connections and the engine are "
            "reachable and that the request found the right identifiers."
        )

    attachments: list[tuple[str, bytes]] = []
    for evidence in await file_service.for_entity(
        session, entity_type="dsar_request", entity_id=request.id,
        purpose="dsar_evidence",
    ):
        if not evidence.is_available:
            continue
        _, payload = await file_service.fetch(session, file_id=evidence.id)
        attachments.append((evidence.filename, payload))

    now = datetime.now(UTC)
    blob = disclosure.build_zip(
        reference=request.reference,
        data=data,
        attachments=attachments,
        produced_at=now,
    )

    previous = request.package_file_id

    stored = await file_service.store(
        session,
        tenant_id=tenant_id,
        purpose="dsar_package",
        entity_type="dsar_request",
        entity_id=request.id,
        filename=f"{request.reference}.zip",
        data=blob,
        declared_content_type="application/zip",
        uploaded_by=actor.id if actor.type == "user" else None,
    )

    request.package_file_id = stored.id
    request.package_assembled_at = now
    request.package_available_until = now + PACKAGE_TTL
    # A new package has not been delivered, whatever happened to the old one.
    request.package_delivered_at = None

    if previous is not None:
        await file_service.purge(
            session, file_id=previous, reason="superseded by a re-assembled package"
        )

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.DSAR_PACKAGE_ASSEMBLED,
        entity_type="dsar_request", entity_id=request.id,
        payload={
            "reference": request.reference,
            "sha256": stored.sha256,
            "byte_size": stored.byte_size,
            "fields": summary["field_count"],
            "collections": summary["collections"],
            "attachments": len(attachments),
            "expires_at": request.package_available_until.isoformat(),
            "replaced_previous": previous is not None,
        },
    )
    await _event(
        session, tenant_id=tenant_id, request=request, actor=actor,
        note=(
            f"Disclosure package assembled: {summary['field_count']} field(s) "
            f"across {len(summary['collections'])} source(s), "
            f"{stored.byte_size} bytes, sha256 {stored.sha256[:12]}…"
        ),
    )
    return stored, summary


async def deliver(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    request: DsarRequest,
    author_user_id: uuid.UUID,
    author_label: str,
    covering_note: str | None = None,
) -> DsarMessage:
    """Put the assembled package in the thread and tell the person it is there.

    Separate from `assemble` on purpose — see the module docstring. The package
    itself is never emailed; the notification says one is waiting.
    """
    if request.package_file_id is None:
        raise DsarRefused(
            "There is no assembled package to deliver. Assemble it first, and "
            "check the preview before you do."
        )

    stored = await session.scalar(
        select(StoredFile).where(StoredFile.id == request.package_file_id)
    )
    if stored is None or not stored.is_available:
        raise DsarRefused(
            "The assembled package is no longer available. Assemble it again."
        )

    note = (covering_note or "").strip() or (
        f"Your request {request.reference} has been completed. The information "
        "we hold about you is attached to this message. It is available until "
        f"{request.package_available_until:%d %B %Y}."
    )

    # `notify=False` here because delivery gets its OWN notification below.
    # The generic "there is a new message" wording would bury the one thing the
    # person is actually waiting for, and would not tell them when it expires.
    message = await post_message(
        session,
        tenant_id=tenant_id,
        actor=actor,
        request=request,
        body=note,
        direction="to_principal",
        author_user_id=author_user_id,
        author_label=author_label,
        notify=False,
    )

    principal = await session.scalar(
        select(DataPrincipal).where(DataPrincipal.id == request.principal_id)
    )
    if principal is not None and principal.email:
        await notification_service.enqueue(
            session,
            tenant_id=tenant_id,
            key="dsar.package_ready",
            to_address=principal.email,
            # Keyed to the message, so re-delivering after a re-assembly sends
            # again rather than being swallowed as a duplicate.
            entity_type="dsar_message",
            entity_id=message.id,
            context={
                "reference": request.reference,
                "expires_on": (
                    f"{request.package_available_until:%d %B %Y}"
                    if request.package_available_until else "further notice"
                ),
            },
        )
        message.notified_at = datetime.now(UTC)

    # Re-home the package onto the message so it appears as that message's
    # attachment, while the request keeps pointing at it too.
    stored.entity_type = "dsar_message"
    stored.entity_id = message.id

    request.package_delivered_at = datetime.now(UTC)

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.DSAR_PACKAGE_DELIVERED,
        entity_type="dsar_request", entity_id=request.id,
        payload={
            "reference": request.reference,
            "sha256": stored.sha256,
            "message_id": str(message.id),
            "expires_at": (
                request.package_available_until.isoformat()
                if request.package_available_until else None
            ),
        },
    )
    return message


async def note_package_downloaded(
    session, *, tenant_id: uuid.UUID, actor: Actor, request: DsarRequest
) -> None:
    """Record a retrieval of the most sensitive object the product produces."""
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.DSAR_PACKAGE_DOWNLOADED,
        entity_type="dsar_request", entity_id=request.id,
        payload={"reference": request.reference},
    )
    await _event(
        session, tenant_id=tenant_id, request=request, actor=actor,
        note="Disclosure package downloaded by the requester",
    )


async def package_for_principal(
    session, *, request: DsarRequest
) -> tuple[StoredFile, bytes]:
    """The bytes, for the person they belong to.

    Expiry is checked here rather than in the route so every caller gets it, and
    an expired package says so rather than 404ing — the person is entitled to
    know it existed and that the window closed.
    """
    if request.package_file_id is None:
        raise NotFound("There is no package for this request yet.")
    if (
        request.package_available_until
        and request.package_available_until <= datetime.now(UTC)
    ):
        raise Conflict(
            "This package has expired. Packages stay available for "
            f"{PACKAGE_TTL.days} days; ask us for another and we will prepare it."
        )
    return await file_service.fetch(session, file_id=request.package_file_id)
