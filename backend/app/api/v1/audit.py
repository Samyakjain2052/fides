"""Audit routes — read and verify. There is no write route, by design."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.api.deps import CurrentUser, require
from app.core.permissions import Capability
from app.models.audit import AuditEvent
from app.schemas.audit import AuditEventOut, AuditPage, ChainStatusOut
from app.services import audit_service

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=AuditPage, summary="Read the audit trail")
async def list_events(
    current: Annotated[CurrentUser, Depends(require(Capability.AUDIT_READ))],
    action: str | None = None,
    entity_type: str | None = None,
    actor_id: uuid.UUID | None = None,
    before_seq: int | None = Query(None, description="Cursor: return entries below this seq."),
    limit: int = Query(50, ge=1, le=200),
) -> AuditPage:
    """Newest first, cursor-paginated.

    No tenant filter is written here — RLS applies it. That is the point of the
    design: a forgotten WHERE clause returns nothing rather than everything.
    """
    stmt = select(AuditEvent)
    count_stmt = select(func.count()).select_from(AuditEvent)

    if action:
        stmt = stmt.where(AuditEvent.action == action)
        count_stmt = count_stmt.where(AuditEvent.action == action)
    if entity_type:
        stmt = stmt.where(AuditEvent.entity_type == entity_type)
        count_stmt = count_stmt.where(AuditEvent.entity_type == entity_type)
    if actor_id:
        stmt = stmt.where(AuditEvent.actor_id == actor_id)
        count_stmt = count_stmt.where(AuditEvent.actor_id == actor_id)
    if before_seq:
        stmt = stmt.where(AuditEvent.seq < before_seq)

    total = (await current.session.execute(count_stmt)).scalar_one()
    rows = list(
        (
            await current.session.execute(
                stmt.order_by(AuditEvent.seq.desc()).limit(limit + 1)
            )
        ).scalars()
    )

    has_more = len(rows) > limit
    page = rows[:limit]
    return AuditPage(
        items=[AuditEventOut.model_validate(r) for r in page],
        total=total,
        next_cursor=page[-1].seq if has_more and page else None,
    )


@router.post("/verify", response_model=ChainStatusOut, summary="Verify chain integrity")
async def verify(
    current: Annotated[CurrentUser, Depends(require(Capability.AUDIT_VERIFY))],
) -> ChainStatusOut:
    """Recompute every hash in this tenant's chain and report the first break.

    Deliberately not cached: a cached "ok" is worthless the moment someone tampers.
    On a large trail this is a slow endpoint, and that is the honest cost of the
    guarantee.
    """
    status = await audit_service.verify_chain(current.session, tenant_id=current.tenant_id)
    return ChainStatusOut(
        ok=status.ok,
        checked=status.checked,
        head_seq=status.head_seq,
        head_hash=status.head_hash,
        first_broken_seq=status.first_broken_seq,
        problem=status.problem,
        verified_at=datetime.now(UTC),
    )
