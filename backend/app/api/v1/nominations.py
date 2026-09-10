"""§14 nomination routes.

The right to nominate somebody to exercise your rights if you die or lose
capacity. No GDPR or US-state equivalent, which is why no comparable product
has these endpoints.

TWO AUDIENCES, TWO GATES

A data principal manages their OWN nomination with `self:read` — it is their
right, and requiring an administrator to record it would put a barrier in front
of a statutory right. Staff need `dsar:process` to see or invoke one, because
invoking transfers the power to extract and destroy somebody's record.

The invoke endpoint is the most consequential in the product. It is deliberately
not reachable by a data principal at all: the person who could legitimately
invoke it is, by the premise, unable to.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, require
from app.api.v1.dsar import _self_principal
from app.core.errors import NotFound
from app.core.permissions import Capability
from app.models.nomination import Nomination
from app.services import nomination_service

router = APIRouter(prefix="/nominations", tags=["nomination (§14)"])


class NominationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nominee_name: str = Field(..., min_length=1, max_length=200)
    nominee_email: str = Field(..., min_length=3, max_length=320)
    nominee_phone: str | None = Field(None, max_length=32)
    nominee_relationship: str | None = Field(None, max_length=120)
    #: `access_only` by default, and that default is a considered one: a
    #: relative settling an estate usually needs records and does not need the
    #: power to destroy them.
    scope: str = "access_only"
    instructions: str | None = Field(None, max_length=4000)
    #: Staff only — recording a nomination somebody made on paper or by phone.
    #: Ignored for a data principal, who can only nominate for themselves.
    principal_id: uuid.UUID | None = None


class InvokeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: What evidence was actually seen. Required, and required to be
    #: substantive — the service refuses anything under 20 characters, because
    #: "yes" is not a record of what somebody examined.
    evidence_note: str = Field(..., min_length=20, max_length=4000)


class NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(None, max_length=4000)


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=4000)


# --------------------------------------------------------------------------- #
# The data principal's own nomination
# --------------------------------------------------------------------------- #

@router.get("/mine", summary="My own nomination, if I have made one")
async def my_nomination(
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_READ))],
) -> dict[str, Any]:
    """Returns the whole history, not just the live one.

    Somebody who revoked a nomination and made another should be able to see
    that they did — a screen showing only the current arrangement makes the past
    unverifiable to the person it was about.
    """
    principal = await _self_principal(current)
    rows = await nomination_service.for_principal(
        current.session, principal_id=principal.id
    )
    return {
        "nominations": [nomination_service.as_dict(r) for r in rows],
        "live": next(
            (nomination_service.as_dict(r) for r in rows if r.is_live), None
        ),
    }


@router.post("", status_code=201, summary="Nominate somebody (§14)")
async def create_nomination(
    body: NominationIn,
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_READ))],
) -> dict[str, Any]:
    """Your own nomination, or — with `dsar:process` — one recorded on somebody
    else's behalf.

    The second case is real: §14 does not require the nomination to be made
    through a web form, and a fiduciary handed a signed nomination on paper has
    to be able to record it. Which of the two happened is on the audit trail.
    """
    staff = Capability.DSAR_PROCESS.value in set(current.capabilities)

    if body.principal_id is not None:
        if not staff:
            # 404 rather than 403: whether a given principal id exists is not
            # something one data principal should learn from another's request.
            raise NotFound("No such person.")
        principal_id = body.principal_id
    else:
        principal_id = (await _self_principal(current)).id

    nomination, token = await nomination_service.create(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        principal_id=principal_id,
        nominee_name=body.nominee_name,
        nominee_email=body.nominee_email,
        nominee_phone=body.nominee_phone,
        nominee_relationship=body.nominee_relationship,
        scope=body.scope,
        instructions=body.instructions,
    )
    out = nomination_service.as_dict(nomination)
    # The token is returned ONCE, to the caller, so an out-of-band route exists
    # if the email does not arrive. It is never stored in plaintext and never
    # returned again.
    out["acceptance_token"] = token
    return out


@router.post("/{nomination_id}/revoke", summary="Withdraw my nomination")
async def revoke_nomination(
    nomination_id: uuid.UUID,
    body: NoteIn,
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_READ))],
) -> dict[str, Any]:
    """No reason required.

    §14 gives a right to nominate, not a right for the nominee to remain
    nominated. Staff may also revoke on the principal's instruction.
    """
    nomination = await nomination_service.get(
        current.session, nomination_id=nomination_id
    )
    staff = Capability.DSAR_PROCESS.value in set(current.capabilities)
    if not staff:
        mine = await _self_principal(current)
        if nomination.principal_id != mine.id:
            raise NotFound("No such nomination.")

    await nomination_service.revoke(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        nomination=nomination, note=body.note,
    )
    return nomination_service.as_dict(nomination)


# --------------------------------------------------------------------------- #
# The nominee
# --------------------------------------------------------------------------- #

@router.post(
    "/{nomination_id}/accept",
    summary="A nominee confirming they know about it",
)
async def accept_nomination(
    nomination_id: uuid.UUID,
    token: str,
    current: Annotated[CurrentUser, Depends(require(Capability.SELF_READ))],
) -> dict[str, Any]:
    """Acceptance does NOT make the nomination valid.

    §14 gives the right to the principal, and it does not depend on the nominee
    agreeing in advance — so an unaccepted nomination stays usable. What
    acceptance buys is a nominee who knows the arrangement exists.
    """
    nomination = await nomination_service.accept(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        nomination_id=nomination_id, token=token,
    )
    return nomination_service.as_dict(nomination)


# --------------------------------------------------------------------------- #
# Staff
# --------------------------------------------------------------------------- #

@router.get("", summary="Nominations in this workspace")
async def list_nominations(
    current: Annotated[CurrentUser, Depends(require(Capability.DSAR_PROCESS))],
    status: str | None = None,
) -> list[dict[str, Any]]:
    query = select(Nomination)
    if status:
        query = query.where(Nomination.status == status)
    rows = (
        await current.session.execute(
            query.order_by(Nomination.created_at.desc())
        )
    ).scalars().all()
    return [nomination_service.as_dict(r) for r in rows]


@router.post(
    "/{nomination_id}/invoke",
    summary="Activate a nomination on evidence of death or incapacity",
)
async def invoke_nomination(
    nomination_id: uuid.UUID,
    body: InvokeIn,
    current: Annotated[CurrentUser, Depends(require(Capability.DSAR_PROCESS))],
) -> dict[str, Any]:
    """The most consequential operation in this product.

    After it, somebody other than the data principal can obtain or destroy
    their entire record. Nothing about it is automated, because nothing can be:
    no software can establish that a person has died. A named member of staff
    records what they saw, the database refuses the state change without it, and
    the evidence goes into the audit chain.

    Deliberately unreachable by a data principal. The person who could
    legitimately invoke this is, by the premise, unable to.
    """
    nomination = await nomination_service.get(
        current.session, nomination_id=nomination_id
    )
    await nomination_service.invoke(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        nomination=nomination, staff_user_id=current.user.id,
        evidence_note=body.evidence_note,
    )
    return nomination_service.as_dict(nomination)


@router.post(
    "/{nomination_id}/retract-invocation",
    summary="Undo an invocation made in error",
)
async def retract_invocation(
    nomination_id: uuid.UUID,
    body: ReasonIn,
    current: Annotated[CurrentUser, Depends(require(Capability.DSAR_PROCESS))],
) -> dict[str, Any]:
    """Distinct from revocation, and the distinction matters.

    Revoking is the principal's decision; this is staff correcting their own
    mistake. Recording a correction as the principal's decision would attribute
    a choice to somebody who — by the premise of the invocation — could not have
    made it.
    """
    nomination = await nomination_service.get(
        current.session, nomination_id=nomination_id
    )
    await nomination_service.retract_invocation(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        nomination=nomination, reason=body.reason,
    )
    return nomination_service.as_dict(nomination)
