"""Notification routes — templates, a preview, and the delivery log.

There is deliberately **no endpoint that sends an arbitrary message**. Every
message this product sends is triggered by a state change that carries an
obligation: a request was received, a consent withdrawn, data is about to be
purged. A "send email to X" route would turn a compliance mailer into a
general-purpose one, and the delivery log's value as evidence rests on every row
in it having a reason.

The preview endpoint renders with sample values instead. Someone editing a
statutory notification needs to see what it will say before it goes to ten
thousand people.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import CurrentUser, require
from app.core.permissions import Capability
from app.models.notification import TEMPLATE_KEYS, Notification, NotificationTemplate
from app.services import notification_providers, notification_service

router = APIRouter(prefix="/notifications", tags=["notifications"])


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    channel: str
    language: str
    subject: str
    body: str
    is_active: bool
    updated_at: Any


class TemplateSave(BaseModel):
    key: str = Field(..., examples=sorted(TEMPLATE_KEYS)[:1])
    channel: str = Field("email", pattern="^(email|sms)$")
    language: str = Field("English", min_length=2, max_length=32)
    subject: str = Field(..., min_length=3, max_length=255)
    body: str = Field(..., min_length=3, max_length=20000)


class TemplateRendered(BaseModel):
    subject: str
    body: str
    placeholders_used: list[str]
    placeholders_available: list[str]


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_key: str
    channel: str
    language: str
    language_requested: str | None
    to_address: str
    subject_rendered: str
    status: str
    provider: str | None
    attempts: int
    last_error: str | None
    suppression_reason: str | None
    entity_type: str | None
    entity_id: uuid.UUID | None
    queued_at: Any
    sent_at: Any | None
    failed_at: Any | None


class ProviderOut(BaseModel):
    """What is actually configured — the first thing a DPO should be told.

    A screen that shows eight healthy templates while the console provider is
    selected is telling a fiduciary they are notifying people when nothing has
    left the building. This is surfaced so the UI cannot avoid saying so.
    """

    name: str
    sends_real_messages: bool
    from_address: str | None
    keys: list[str]


@router.get("/provider", response_model=ProviderOut,
            summary="Which provider is configured, and whether it actually sends")
async def provider(
    current: Annotated[CurrentUser, Depends(require(Capability.NOTIFICATION_MANAGE))],
) -> ProviderOut:
    from app.core.config import get_settings

    settings = get_settings()
    impl = notification_providers.get_provider()
    return ProviderOut(
        name=impl.name,
        # The console provider logs and returns success. Everything downstream
        # looks identical to a real send, which is exactly why this flag exists.
        sends_real_messages=impl.name != "console",
        from_address=settings.notification_from_address,
        keys=sorted(TEMPLATE_KEYS),
    )


class MyNotificationOut(BaseModel):
    """What a data principal is shown about themselves.

    A narrower shape than the admin view on purpose. `provider`, `attempts` and
    `last_error` are operational detail about our mail plumbing; the person on the
    receiving end is owed the fact and the timing, not our retry history.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_key: str
    channel: str
    language: str
    subject_rendered: str
    status: str
    # Included so a screen can tie a message to the request it was about without
    # substring-matching the subject line — which breaks silently the moment a
    # template stops including {{reference}}.
    entity_type: str | None
    entity_id: uuid.UUID | None
    queued_at: Any
    sent_at: Any | None


@router.get("/mine", response_model=list[MyNotificationOut],
            summary="What this platform has told me, and when")
async def my_notifications(
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_READ))],
) -> Any:
    """A data principal's own notification history.

    Worth having as a first-class endpoint rather than an admin screen only: "you
    were informed on the 14th" is a claim made *about* somebody, and the person it
    is made about should be able to see it without asking. It also makes the
    fallback visible from their side — if they asked for Hindi and got English,
    that is on their record too.

    Scoped to the principal record derived from the signed-in user, so it cannot
    be widened by passing an id.
    """
    from app.api.v1.dsar import _self_principal

    principal = await _self_principal(current)
    return await notification_service.log_for_tenant(
        current.session, current.tenant_id, principal_id=principal.id, limit=50
    )


@router.get("/templates", response_model=list[TemplateOut], summary="List templates")
async def list_templates(
    current: Annotated[CurrentUser, Depends(require(Capability.NOTIFICATION_MANAGE))],
) -> list[NotificationTemplate]:
    return await notification_service.templates_for_tenant(
        current.session, current.tenant_id
    )


@router.put("/templates", response_model=TemplateOut,
            summary="Create or update a template (placeholders validated here)")
async def save_template(
    body: TemplateSave,
    current: Annotated[CurrentUser, Depends(require(Capability.NOTIFICATION_MANAGE))],
) -> NotificationTemplate:
    """Rejects unknown placeholders at save time.

    This is the only moment a human is looking at the template. A placeholder
    that renders as an empty string turns "due on 14 September" into "due on ",
    silently, in a notification with a statutory deadline in it.
    """
    return await notification_service.upsert_template(
        current.session,
        tenant_id=current.tenant_id,
        key=body.key,
        channel=body.channel,
        language=body.language,
        subject=body.subject,
        body=body.body,
    )


@router.post("/templates/preview", response_model=TemplateRendered,
             summary="Render a template with sample values — sends nothing")
async def preview_template(
    body: TemplateSave,
    current: Annotated[CurrentUser, Depends(require(Capability.NOTIFICATION_MANAGE))],
) -> TemplateRendered:
    """Validate, then render with placeholder-shaped samples.

    Validation runs first so the preview cannot show a plausible-looking message
    for a template that would be refused on save.
    """
    notification_service.validate_template(body.key, body.subject, body.body)

    available = TEMPLATE_KEYS[body.key]
    sample = {name: f"[{name}]" for name in available}
    tenant_name = getattr(current, "tenant_name", None)
    sample["organisation"] = tenant_name or "[organisation]"

    used = notification_service.placeholders_in(body.subject) | \
        notification_service.placeholders_in(body.body)
    return TemplateRendered(
        subject=notification_service.render(body.subject, sample),
        body=notification_service.render(body.body, sample),
        placeholders_used=sorted(used),
        placeholders_available=sorted(available),
    )


@router.get("/log", response_model=list[NotificationOut],
            summary="The delivery log: what was sent, what failed, what was suppressed and why")
async def delivery_log(
    current: Annotated[CurrentUser, Depends(require(Capability.NOTIFICATION_MANAGE))],
    principal_id: uuid.UUID | None = None,
    status: str | None = Query(None, pattern="^(queued|sending|delivered|failed|suppressed)$"),
    limit: int = Query(100, ge=1, le=500),
) -> list[Notification]:
    """Suppressed rows are returned alongside delivered ones, on purpose.

    "We never told them, because we hold no address for them" is an answer a
    fiduciary needs to be able to give. A log filtered down to successes would
    hide precisely the rows worth looking at.
    """
    return await notification_service.log_for_tenant(
        current.session,
        current.tenant_id,
        principal_id=principal_id,
        status=status,
        limit=limit,
    )


@router.post("/log/{notification_id}/retry", response_model=NotificationOut,
             summary="Re-attempt one failed message")
async def retry(
    notification_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.NOTIFICATION_MANAGE))],
) -> Notification:
    """Requeue a failed message and attempt it once, now.

    Retry does not reset `attempts`. The count is the record of how hard we tried
    to reach somebody, and a counter a human can zero is not that record.
    """
    return await notification_service.retry_failed(
        current.session, tenant_id=current.tenant_id, notification_id=notification_id
    )


class WorkerResult(BaseModel):
    claimed: int
    delivered: int
    failed: int
    requeued: int


@router.post("/drain", response_model=WorkerResult,
             summary="Process due messages now (the worker's loop, run once)")
async def drain(
    current: Annotated[CurrentUser, Depends(require(Capability.NOTIFICATION_MANAGE))],
    limit: int = Query(25, ge=1, le=100),
) -> WorkerResult:
    """One pass of the background worker, triggered by hand.

    Exists because there is no scheduler deployed yet: without it a message that
    hit a transient failure would sit in `queued` forever and the retry path would
    be untestable outside the suite. Tenant-scoped, unlike the real worker — an
    admin draining their own queue must not touch anybody else's.
    """
    return WorkerResult(
        **await notification_service.drain_tenant(
            current.session, tenant_id=current.tenant_id, limit=limit
        )
    )
