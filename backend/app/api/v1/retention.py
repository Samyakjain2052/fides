"""Retention routes.

**Preview is the primary action.** The live run is separate, requires the policy
name back verbatim, and is the only endpoint in this API that destroys anything.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import CurrentUser, require
from app.core.permissions import Capability
from app.models.retention import PurgeRun, RetentionPolicy
from app.services import retention_service

router = APIRouter(prefix="/retention", tags=["retention"])


class PolicyCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)
    data_category: str = Field(..., min_length=2, max_length=128,
                               examples=["Contact Data"])
    retention_days: int = Field(..., ge=1, le=36500)
    action: str = Field(
        "mask",
        pattern="^(mask|delete)$",
        description="mask nulls the identifiers and keeps the row, matching the "
                    "DSAR erasure path. Two meanings of 'erased' in one product "
                    "would be an audit contradiction.",
    )
    auto_delete: bool = Field(
        False,
        description="Off unless deliberately switched on. A policy that destroys "
                    "on a timer must also carry a notice period.",
    )
    notify_days: int = Field(14, ge=0, le=365)
    exemption_code: str = Field("none", pattern="^(none|statutory|legal_hold|dispute)$")
    exemption_reference: str | None = Field(
        None, max_length=255, examples=["RBI KYC Master Direction 2016 §12"]
    )


class PolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    data_category: str
    retention_days: int
    action: str
    auto_delete: bool
    notify_days: int
    exemption_code: str
    exemption_reference: str | None
    exemption_expires_at: Any | None
    is_active: bool
    last_run_at: Any | None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    policy_id: uuid.UUID
    mode: str
    status: str
    started_at: Any
    finished_at: Any | None
    candidates_found: int
    rows_affected: int
    scope_summary: dict[str, Any] | None
    error: str | None


class RunItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    table_name: str
    entity_id: uuid.UUID
    action_taken: str
    skip_reason: str | None


class LiveRun(BaseModel):
    confirm: str = Field(
        ...,
        description="The policy's name, exactly. An irreversible action needs a "
                    "step that cannot be taken by a mis-click or by a script that "
                    "meant to call preview.",
    )


@router.get("/policies", response_model=list[PolicyOut], summary="List retention policies")
async def list_policies(
    current: Annotated[CurrentUser, Depends(require(Capability.RETENTION_MANAGE))],
) -> list[RetentionPolicy]:
    return await retention_service.list_policies(current.session, current.tenant_id)


@router.post("/policies", response_model=PolicyOut, status_code=201,
             summary="Create a retention policy")
async def create_policy(
    body: PolicyCreate,
    current: Annotated[CurrentUser, Depends(require(Capability.RETENTION_MANAGE))],
) -> RetentionPolicy:
    return await retention_service.create_policy(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        name=body.name,
        data_category=body.data_category,
        retention_days=body.retention_days,
        action=body.action,
        auto_delete=body.auto_delete,
        notify_days=body.notify_days,
        exemption_code=body.exemption_code,
        exemption_reference=body.exemption_reference,
    )


class PolicyUpdate(BaseModel):
    """Every field optional — a PATCH, so an untouched field keeps its value.

    `data_category` is absent deliberately: changing it would point a policy's
    history and its purge receipts at a different set of people. That is a new
    policy, not an edit.

    `extra="forbid"` matters here. Pydantic's default is to drop fields it does not
    declare, so a request asking to change the category returned 200 while
    changing nothing — the caller believed it had worked and the service-level
    refusal was unreachable. A silent success is worse than a refusal, so an
    unknown field is now a 422 that names it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=2, max_length=255)
    retention_days: int | None = Field(None, ge=1, le=36500)
    action: str | None = Field(None, pattern="^(mask|delete)$")
    auto_delete: bool | None = None
    notify_days: int | None = Field(None, ge=0, le=365)
    exemption_code: str | None = Field(
        None, pattern="^(none|statutory|legal_hold|dispute)$"
    )
    exemption_reference: str | None = Field(None, max_length=255)
    is_active: bool | None = None
    confirm_shortening: bool = Field(
        False,
        description="Required when shortening the window on an auto-delete policy. "
                    "That edit enlarges an unattended destruction set without "
                    "anybody pressing anything, so it does not follow from an "
                    "ordinary form save.",
    )


@router.patch("/policies/{policy_id}", response_model=PolicyOut,
              summary="Edit a policy — same validation as creating one")
async def update_policy(
    policy_id: uuid.UUID,
    body: PolicyUpdate,
    current: Annotated[CurrentUser, Depends(require(Capability.RETENTION_MANAGE))],
) -> RetentionPolicy:
    """Before this, a policy could only be created and run.

    The screen said "create a replacement in the meantime", which leaves two
    policies over one category — and the older one keeps purging on its own terms.
    That is worse than an edit, not safer.

    Every change is audited with its before and after, and a shortened window is
    flagged as such in the entry so a reader does not have to infer the direction
    from the numbers.
    """
    patch = body.model_dump(exclude={"confirm_shortening"}, exclude_none=True)
    return await retention_service.update_policy(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        policy_id=policy_id,
        confirm_shortening=body.confirm_shortening,
        **patch,
    )


@router.post(
    "/policies/{policy_id}/preview", response_model=RunOut,
    summary="Dry run — reports exactly what WOULD be purged, and touches nothing",
)
async def preview(
    policy_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.RETENTION_MANAGE))],
) -> PurgeRun:
    """The primary action.

    Uses the same candidate-selection path as the live run — deliberately the
    same function, not an equivalent one, because a preview that can disagree
    with the executor is how a run reports four rows and destroys four hundred.
    """
    return await retention_service.preview(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        policy_id=policy_id,
    )


@router.post(
    "/policies/{policy_id}/run", response_model=RunOut,
    summary="LIVE run — irreversible. Requires the policy name as confirmation.",
)
async def run(
    policy_id: uuid.UUID,
    body: LiveRun,
    current: Annotated[CurrentUser, Depends(require(Capability.RETENTION_MANAGE))],
) -> PurgeRun:
    """The only endpoint in this API that destroys data.

    Refuses if the policy is inactive or carries a live exemption — honouring a
    decision somebody already made beats relying on whoever presses the button to
    remember why it was set.
    """
    return await retention_service.execute(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        policy_id=policy_id, confirm=body.confirm,
    )


@router.get("/runs", response_model=list[RunOut], summary="Purge history")
async def list_runs(
    current: Annotated[CurrentUser, Depends(require(Capability.RETENTION_MANAGE))],
    policy_id: uuid.UUID | None = None,
) -> list[PurgeRun]:
    return await retention_service.list_runs(
        current.session, current.tenant_id, policy_id=policy_id
    )


@router.get("/runs/{run_id}/items", response_model=list[RunItemOut],
            summary="The receipt: every row touched, and every one skipped with its reason")
async def run_items(
    run_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.RETENTION_MANAGE))],
) -> Any:
    return await retention_service.run_items(current.session, current.tenant_id, run_id)
