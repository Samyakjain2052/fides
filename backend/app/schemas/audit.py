"""Audit read shapes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    seq: int
    actor_type: str
    actor_id: uuid.UUID | None
    actor_label: str | None
    action: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    payload: dict[str, Any]
    ip_address: str | None
    prev_hash: str
    hash: str
    created_at: datetime


class AuditPage(BaseModel):
    items: list[AuditEventOut]
    total: int
    next_cursor: int | None = Field(
        None, description="Pass as `before_seq` for the next page. Cursor rather than "
                          "offset: this table only grows, and offset pagination "
                          "shifts under inserts."
    )


class ChainStatusOut(BaseModel):
    ok: bool
    checked: int
    head_seq: int | None
    head_hash: str | None
    first_broken_seq: int | None = None
    problem: str | None = None
    verified_at: datetime
