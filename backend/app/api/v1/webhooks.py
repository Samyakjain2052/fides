"""Outbound alerts — subscribers, the delivery log, and replay.

`webhook:manage` for anything that changes where alerts go, `webhook:read` for
the log. The split is not cosmetic: whoever can edit an endpoint chooses the URL
every future withdrawal alert is posted to, with our signature on it. An auditor
needs to answer "was this processor told to stop, and did they confirm" and needs
none of that.

No route returns a signing secret except the two that mint one — creation and
rotation — and each returns it exactly once. `webhook_service.endpoint_out`
cannot serialise it at all, which is a stronger guarantee than a router that
remembers to leave it out.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import CurrentUser, require
from app.core.permissions import Capability
from app.models.webhook import WEBHOOK_EVENTS
from app.services import webhook_service

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class EndpointIn(BaseModel):
    # `forbid` — a typo in a field name is a 422 rather than a silently dropped
    # value. The same reasoning connections give: a dropped `events` list would
    # create an endpoint that looks configured and receives nothing.
    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., max_length=2048)
    label: str = Field(..., min_length=1, max_length=120)
    events: list[str] = Field(..., min_length=1)
    description: str | None = Field(None, max_length=2000)
    #: Hours the receiver has to confirm it acted before the alert is escalated
    #: to the DPO. Bounded below at 1 because a deadline shorter than a retry
    #: cycle would escalate alerts that are still being delivered.
    ack_deadline_hours: int = Field(24, ge=1, le=720)


class EndpointPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool


class AcknowledgeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(None, max_length=500)


@router.get("/events", summary="Every event an endpoint can subscribe to")
async def list_events(
    current: Annotated[CurrentUser, Depends(require(Capability.WEBHOOK_READ))],
) -> dict[str, Any]:
    """The closed set, with what each one means.

    Served rather than hardcoded in the console for the same reason the
    connector catalogue is: the UI cannot then offer a subscription the backend
    has never heard of.
    """
    return {
        "events": [
            {"id": key, "description": description}
            for key, description in sorted(WEBHOOK_EVENTS.items())
        ],
        "signature_header": webhook_service.SIGNATURE_HEADER,
        "signature_tolerance_seconds": webhook_service.SIGNATURE_TOLERANCE_SECONDS,
    }


@router.get("", summary="This workspace's subscribers")
async def list_endpoints(
    current: Annotated[CurrentUser, Depends(require(Capability.WEBHOOK_READ))],
) -> list[dict[str, Any]]:
    rows = await webhook_service.list_endpoints(
        current.session, tenant_id=current.tenant_id
    )
    return [webhook_service.endpoint_out(row) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED, summary="Register a subscriber")
async def create_endpoint(
    body: EndpointIn,
    current: Annotated[CurrentUser, Depends(require(Capability.WEBHOOK_MANAGE))],
) -> dict[str, Any]:
    """Registers an endpoint and returns its signing secret — once.

    The secret is not recoverable afterwards. If it is lost, rotate: that is a
    deliberate choice, not an omission. A secret a support request can retrieve
    is a secret a support request can leak, and this one authorises "stop
    processing" instructions into a customer's systems.
    """
    endpoint, secret = await webhook_service.create_endpoint(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        url=body.url,
        label=body.label,
        events=body.events,
        description=body.description,
        ack_deadline_hours=body.ack_deadline_hours,
    )
    return {
        **webhook_service.endpoint_out(endpoint),
        "secret": secret,
        "secret_shown_once": True,
    }


@router.patch("/{endpoint_id}", summary="Enable or disable a subscriber")
async def set_active(
    endpoint_id: uuid.UUID,
    body: EndpointPatch,
    current: Annotated[CurrentUser, Depends(require(Capability.WEBHOOK_MANAGE))],
) -> dict[str, Any]:
    endpoint = await webhook_service.set_active(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        endpoint_id=endpoint_id,
        active=body.active,
    )
    return webhook_service.endpoint_out(endpoint)


@router.post("/{endpoint_id}/rotate-secret", summary="Issue a new signing secret")
async def rotate_secret(
    endpoint_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.WEBHOOK_MANAGE))],
) -> dict[str, Any]:
    """The old secret stops working immediately — there is no overlap window.

    Alerts signed with the new secret will be rejected by a receiver still
    holding the old one, and those alerts retry. A minute of rejections is the
    price of not leaving a leaked secret valid for the length of a grace period.
    """
    secret = await webhook_service.rotate_secret(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        endpoint_id=endpoint_id,
    )
    return {"secret": secret, "secret_shown_once": True}


@router.delete(
    "/{endpoint_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a subscriber and its delivery history",
)
async def delete_endpoint(
    endpoint_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.WEBHOOK_MANAGE))],
) -> Response:
    """Discards the evidence that this processor was ever told anything.

    Usually the wrong act. Disabling keeps the log — "we told them on the 14th
    and they confirmed" — while stopping future sends, and decommissioning a
    processor is nearly always the disable case.
    """
    await webhook_service.delete_endpoint(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        endpoint_id=endpoint_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/deliveries", summary="What was sent, and what came back")
async def list_deliveries(
    current: Annotated[CurrentUser, Depends(require(Capability.WEBHOOK_READ))],
    endpoint_id: uuid.UUID | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    unacknowledged_only: bool = False,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return await webhook_service.list_deliveries(
        current.session,
        tenant_id=current.tenant_id,
        endpoint_id=endpoint_id,
        status=status_filter,
        unacknowledged_only=unacknowledged_only,
        limit=limit,
    )


@router.post("/deliveries/{delivery_id}/replay", summary="Send a failed alert again")
async def replay(
    delivery_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.WEBHOOK_MANAGE))],
) -> dict[str, Any]:
    """Queues a NEW attempt carrying the original payload.

    The failed row stays failed. It is the evidence that there was a window in
    which a processor was not reachable, and a replay that overwrote it would
    remove the only record of the gap.
    """
    row = await webhook_service.replay(
        current.session, tenant_id=current.tenant_id, delivery_id=delivery_id
    )
    return webhook_service.delivery_out(row)


@router.post(
    "/deliveries/{delivery_id}/acknowledge",
    summary="Record that the receiver acted on an alert",
)
async def acknowledge(
    delivery_id: uuid.UUID,
    body: AcknowledgeIn,
    current: Annotated[CurrentUser, Depends(require(Capability.WEBHOOK_MANAGE))],
) -> dict[str, Any]:
    """The console's copy of the acknowledgement, for a receiver that cannot call back.

    The receiver's own route is on the public API, authenticated with their
    secret key — that is the one that carries weight, because the confirmation
    comes from the party that did the work. This route exists for the real case
    where a processor confirms by email or over the phone, and a DPO needs the
    record to say so rather than leaving it permanently unacknowledged.

    The audit chain distinguishes them: this one has a named user as the actor.
    """
    row = await webhook_service.acknowledge(
        current.session,
        tenant_id=current.tenant_id,
        delivery_id=delivery_id,
        note=body.note,
    )
    return webhook_service.delivery_out(row)
