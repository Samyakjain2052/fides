"""DSAR request/response shapes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DsarSubmit(BaseModel):
    """Raise a rights request.

    Note what is NOT here: `deadline_at`. The statutory clock is computed by the
    server from the workspace's SLA, because a deadline a caller can set is not a
    deadline anyone can rely on.
    """

    principal_id: uuid.UUID | None = Field(
        None,
        description="Whose data. Omit when raising your own request — the "
                    "signed-in identity is used.",
    )
    type: Literal["access", "erasure", "correction"]
    verification_method: Literal["otp", "digilocker", "staff_verified", "session"] | None = None
    correction_payload: dict[str, Any] | None = Field(
        None,
        description="Correction only: what is wrong and what it should be. The "
                    "engine has no correction action, so this is worked by hand "
                    "against the same deadline.",
        examples=[{"field": "phone", "current": "+91 90000 00000",
                   "corrected": "+91 98765 43210"}],
    )


class DsarStatusChange(BaseModel):
    to_status: Literal["verifying", "in_progress", "completed", "rejected", "cancelled"]
    reason: str | None = Field(
        None, max_length=2000,
        description="Required when rejecting. A rejection with no recorded reason "
                    "is not a decision anyone can defend.",
    )
    note: str | None = Field(None, max_length=2000)


class DsarEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    actor_type: str
    actor_label: str | None
    from_status: str | None
    to_status: str | None
    note: str | None
    automated: bool


class DsarOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    reference: str
    principal_id: uuid.UUID
    type: str
    status: str
    engine_ref: str | None
    engine_status: str | None
    engine_error: str | None
    submitted_at: datetime
    deadline_at: datetime
    resolved_at: datetime | None
    verification_method: str | None
    verified_at: datetime | None
    requested_by_actor: str
    rejection_reason: str | None
    correction_payload: dict[str, Any] | None
    package_available_until: datetime | None


class DsarDetail(DsarOut):
    principal_ref: str | None = None
    principal_email: str | None = None
    timeline: list[DsarEventOut] = Field(default_factory=list)

    # Derived, so a screen does not have to re-implement the state machine and
    # then disagree with the server about what is possible.
    allowed_transitions: list[str] = Field(default_factory=list)
    overdue: bool = False
    days_remaining: int | None = None


class DsarPage(BaseModel):
    items: list[DsarDetail]
    total: int
