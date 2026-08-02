"""Auth request/response shapes.

Pydantic schemas are separate from the ORM models on purpose: returning a model
directly is how `password_hash` ends up in a JSON response. What crosses the wire
is declared explicitly, field by field.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    tenant_slug: str = Field(..., min_length=1, max_length=63, examples=["acme-fintech"])
    email: EmailStr
    # No max on password length beyond sanity: truncating a passphrase silently
    # weakens it. 8 is a floor, not a policy — real policy belongs in a validator.
    password: str = Field(..., min_length=8, max_length=256)


class RegisterRequest(BaseModel):
    """Self-serve signup: a company and its first Admin/DPO, together."""

    company_name: str = Field(
        ..., min_length=2, max_length=255, examples=["Acme Fintech Pvt. Ltd."],
        description="Your organisation's name, as the Data Fiduciary.",
    )
    workspace: str | None = Field(
        None, min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$",
        examples=["acme-fintech"],
        description="URL-safe workspace id used at sign-in. Derived from the "
                    "company name if you leave it blank.",
    )
    admin_name: str = Field(..., min_length=2, max_length=255, examples=["Amit Kumar"])
    admin_email: EmailStr = Field(..., description="Becomes the first Admin/DPO account.")
    password: str = Field(
        ..., min_length=12, max_length=256,
        description="At least 12 characters. A passphrase of a few unrelated words "
                    "beats a short complex string.",
    )


class SlugCheck(BaseModel):
    workspace: str
    available: bool
    reason: str | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: str
    tenant_id: uuid.UUID
    mfa_enabled: bool
    last_login_at: datetime | None = None


class TokenResponse(BaseModel):
    """The refresh token is deliberately absent.

    It goes back as an HttpOnly cookie and never appears in a JSON body, because
    anything JavaScript can read, an XSS payload can steal.
    """

    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserOut
    capabilities: list[str] = Field(
        default_factory=list,
        description="What this role may do. The UI uses it to render; the server "
        "enforces it regardless.",
    )
