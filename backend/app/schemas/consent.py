"""Consent-domain request/response shapes.

Separate from the ORM models, as everywhere else in this codebase: what crosses
the wire is declared field by field, so a column added later cannot leak by
accident.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# --------------------------------------------------------------------------- #
# Purposes
# --------------------------------------------------------------------------- #

class PurposeCreate(BaseModel):
    key: str = Field(
        ..., min_length=2, max_length=64, examples=["marketing_email"],
        description="Stable machine key. Your systems pass this to /consent/check, "
                    "so choose it carefully — it should never change.",
    )
    name: str = Field(..., min_length=2, max_length=255, examples=["Marketing communications"])
    category: str = Field(..., min_length=2, max_length=128, examples=["Contact Data"])
    is_mandatory: bool = Field(
        False,
        description="Shown with a locked toggle and the reason, never hidden. "
                    "A mandatory purpose may not use 'consent' as its legal basis.",
    )
    legal_basis: Literal["consent", "legitimate_use", "legal_obligation", "vital_interest"] = "consent"
    retention_days: int | None = Field(None, ge=1, le=36500)


class PurposeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    name: str
    category: str
    is_mandatory: bool
    legal_basis: str
    retention_days: int | None
    is_active: bool


# --------------------------------------------------------------------------- #
# Notices
# --------------------------------------------------------------------------- #

class NoticeCreate(BaseModel):
    purpose_id: uuid.UUID
    language: str = Field("English", max_length=32)
    content: str = Field(..., min_length=10)
    data_collected: str = Field(..., min_length=2)
    user_rights: str = Field(..., min_length=2)
    withdrawal_policy: str = Field(..., min_length=2)


class NoticeRevise(BaseModel):
    """Any field left out keeps its current wording.

    On a published notice this creates the next version rather than editing —
    see notice_service.revise_notice.
    """

    content: str | None = Field(None, min_length=10)
    data_collected: str | None = Field(None, min_length=2)
    user_rights: str | None = Field(None, min_length=2)
    withdrawal_policy: str | None = Field(None, min_length=2)


class NoticeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    purpose_id: uuid.UUID
    version: int
    language: str
    content: str
    data_collected: str
    user_rights: str
    withdrawal_policy: str
    published_at: datetime | None

    @property
    def is_draft(self) -> bool:
        return self.published_at is None


# --------------------------------------------------------------------------- #
# Data principals
# --------------------------------------------------------------------------- #

class PrincipalCreate(BaseModel):
    external_id: str = Field(
        ..., min_length=1, max_length=255,
        description="Your own identifier for this person, so your systems can ask "
                    "about them without an id exchange.",
    )
    email: EmailStr | None = None
    phone: str | None = Field(None, max_length=32)
    is_minor: bool = False
    guardian_email: EmailStr | None = Field(
        None, description="Required when is_minor — DPDP §9 needs verifiable "
                          "parental consent before a child's data is processed.",
    )


class PrincipalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    external_id: str
    email: str | None
    phone: str | None
    is_minor: bool
    guardian_email: str | None
    verified_at: datetime | None


# --------------------------------------------------------------------------- #
# Consents
# --------------------------------------------------------------------------- #

class ConsentGrant(BaseModel):
    principal_id: uuid.UUID
    purpose_id: uuid.UUID
    language: str = Field("English", max_length=32)
    method: Literal["checkbox", "banner", "api", "import", "guardian", "verbal_logged"] = "checkbox"
    source: str | None = Field(None, max_length=128, examples=["preference-centre"])
    notice_id: uuid.UUID | None = Field(
        None,
        description="The notice version actually shown. Pass it when you know: if a "
                    "newer version is published while someone is reading, the record "
                    "must name the text they saw, not the text that replaced it.",
    )


class ConsentWithdraw(BaseModel):
    principal_id: uuid.UUID
    purpose_id: uuid.UUID
    reason: str | None = Field(None, max_length=500)


class ConsentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    principal_id: uuid.UUID
    purpose_id: uuid.UUID
    notice_id: uuid.UUID
    status: str
    given_at: datetime | None
    withdrawn_at: datetime | None
    expires_at: datetime | None
    language: str
    method: str
    source: str | None


class ConsentDetail(ConsentOut):
    """A consent with the purpose and the exact notice version it was given against.

    The joined fields are the difference between a row and evidence: on their
    own, three UUIDs cannot answer "consented to what, in which words?".
    """

    purpose_key: str
    purpose_name: str
    is_mandatory: bool
    notice_version: int
    notice_content: str


class ConsentCheckOut(BaseModel):
    """The answer to the only question a customer's systems ask in real time."""

    allowed: bool
    status: str = Field(..., examples=["active", "withdrawn", "expired", "never_given"])
    purpose: str
    given_at: datetime | None = None
    withdrawn_at: datetime | None = None
    expires_at: datetime | None = None
    language: str | None = None
    reason: str | None = Field(
        None, description="Why not, when allowed is false. Null when allowed."
    )


class ConsentHistoryEntry(BaseModel):
    """One line of consent history, read from the audit chain."""

    seq: int
    action: str
    occurred_at: datetime
    actor_type: str
    payload: dict
    hash: str
