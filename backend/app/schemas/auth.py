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
