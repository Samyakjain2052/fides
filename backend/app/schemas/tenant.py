"""Tenant, user and API-key shapes."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class TenantCreate(BaseModel):
    slug: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]{1,62}$",
                      description="Lowercase, url-safe. Used at login and in banner keys.")
    name: str = Field(..., min_length=1, max_length=255)
    legal_name: str | None = Field(None, max_length=255)
    admin_email: EmailStr
    admin_name: str = Field(..., min_length=1, max_length=255)
    admin_password: str = Field(..., min_length=12, max_length=256,
                               description="First admin's password. 12 char minimum.")


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    legal_name: str | None
    default_language: str
    dsar_sla_days: int
    grievance_sla_days: int
    grievance_escalation_days: int
    require_mfa: bool
    is_active: bool
    created_at: datetime


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=1, max_length=255)
    role: str = Field(..., examples=["admin", "auditor", "grievance_officer", "data_principal"])
    password: str | None = Field(None, min_length=12, max_length=256)


class RoleChange(BaseModel):
    role: str


class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, examples=["marketing-service"])
    scopes: list[str] = Field(..., min_length=1,
                              examples=[["consent:read"]],
                              description="Least privilege. A key that only checks consent "
                                          "should not hold consent:write.")
    environment: str = Field("live", pattern="^(live|test)$")
    expires_in_days: int | None = Field(None, ge=1, le=3650)


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    prefix: str
    environment: str
    scopes: list[str]
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyOut):
    """Returned once, at creation."""

    api_key: str = Field(..., description="Shown ONCE. Only a hash is stored — we cannot "
                                          "show it again. Store it now or rotate the key.")
