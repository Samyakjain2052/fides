"""Connections to a customer's own systems.

`connection:manage` throughout, which is admin-only. Not granted to the auditor
despite their broad read access: a list of a company's production systems, hosts
and credential hints is infrastructure intelligence, and read-only is not the
same as harmless.

No route returns a decrypted credential. The response shape comes from
`connection_service._out`, which cannot serialise one.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import CurrentUser, require
from app.connectors import registry
from app.core.permissions import Capability
from app.services import connection_service

router = APIRouter(prefix="/connections", tags=["connections"])


class ConnectionIn(BaseModel):
    # `forbid`, so a typo in a field name is a 422 rather than a silently
    # dropped credential. This product has been bitten by Pydantic's default
    # before: a `data_category` change appeared to succeed because the field was
    # discarded before the service could refuse it.
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(..., max_length=64)
    label: str = Field("", max_length=120)
    values: dict[str, Any] = Field(default_factory=dict)


class ConnectionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(None, max_length=120)
    #: A secret left blank means "unchanged" — the form cannot display what is
    #: already stored, so blank cannot mean "clear it".
    values: dict[str, Any] | None = None


@router.get("/catalog", summary="Everything this product can connect to")
async def catalog(
    current: Annotated[CurrentUser, Depends(require(Capability.CONNECTION_MANAGE))],
) -> dict[str, Any]:
    """The registry, verbatim.

    The admin screen renders itself from this — fields, labels, help text and
    status badge — so the UI cannot offer a connector the backend does not have,
    or present one as usable when its status says otherwise.
    """
    items = registry.as_catalog()
    return {
        "items": items,
        # Precomputed so the screen does not have to derive the honest headline
        # figure, and so it is the same number everywhere it appears.
        "counts": {
            "total": len(items),
            "storable": sum(1 for i in items if i["storable"]),
            "by_status": {
                s.value: sum(1 for i in items if i["status"] == s.value)
                for s in registry.Status
            },
        },
    }


@router.get("", summary="This workspace's connections")
async def list_connections(
    current: Annotated[CurrentUser, Depends(require(Capability.CONNECTION_MANAGE))],
) -> list[dict[str, Any]]:
    return await connection_service.list_for_tenant(
        current.session, tenant_id=current.tenant_id
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="Add a connection")
async def create_connection(
    body: ConnectionIn,
    current: Annotated[CurrentUser, Depends(require(Capability.CONNECTION_MANAGE))],
) -> dict[str, Any]:
    """Stores credentials. Does NOT claim the connection works.

    The row comes back `unverified`; only `POST /{id}/test` can make it
    `connected`.
    """
    return await connection_service.create(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        connector_id=body.connector_id,
        label=body.label,
        values=body.values,
        created_by=current.user.id,
    )


@router.patch("/{connection_id}", summary="Edit a connection")
async def update_connection(
    connection_id: uuid.UUID,
    body: ConnectionPatch,
    current: Annotated[CurrentUser, Depends(require(Capability.CONNECTION_MANAGE))],
) -> dict[str, Any]:
    return await connection_service.update(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        connection_id=connection_id,
        label=body.label,
        values=body.values,
    )


@router.post("/{connection_id}/test", summary="Actually connect, and record it")
async def test_connection(
    connection_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.CONNECTION_MANAGE))],
) -> dict[str, Any]:
    """The point of the feature.

    Audited on failure as well as success — a connection that stopped working is
    a fact about a company's DSAR reach, and finding out during a statutory
    deadline is too late.
    """
    return await connection_service.test(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        connection_id=connection_id,
    )


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Remove a connection and its credential")
async def delete_connection(
    connection_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.CONNECTION_MANAGE))],
) -> Response:
    # `-> Response`, not `-> None`. FastAPI builds a response model from the
    # return annotation and then asserts that a 204 has no body, so `-> None`
    # fails at import time — the same shape `logout` already uses.
    await connection_service.delete(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        connection_id=connection_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
