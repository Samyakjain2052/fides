"""
The audit service — the only way anything gets written to the trail.

Every state change in the product routes through `record()`. It runs inside the
caller's transaction, so an audit entry and the change it describes either both
commit or both roll back. That property is why the trail can be trusted: there is
no window in which the data changed but the evidence didn't.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import GENESIS_HASH, audit_hash
from app.models.audit import AuditEvent

logger = logging.getLogger("app.audit")

# Namespace for the per-tenant advisory lock. Any constant works; it just has to
# be stable so every writer contends on the same key.
_LOCK_NAMESPACE = 0x44_53_41_55  # "DSAU"


@dataclass(frozen=True)
class Actor:
    """Who is doing the thing.

    `type` maps to the "initiator" column a regulator asks about: was this the
    person themselves, staff, an integration, or the system acting on a schedule?
    """

    type: str          # user | api_key | system | data_principal
    id: uuid.UUID | None = None
    label: str | None = None
    ip: str | None = None
    user_agent: str | None = None

    @classmethod
    def system(cls, label: str = "system") -> Actor:
        return cls(type="system", label=label)


@dataclass(frozen=True)
class ChainStatus:
    ok: bool
    checked: int
    head_seq: int | None
    head_hash: str | None
    first_broken_seq: int | None = None
    problem: str | None = None


async def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    action: str,
    payload: dict[str, Any] | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
) -> AuditEvent:
    """Append one entry to a tenant's chain.

    Must be called inside an open transaction. Takes a per-tenant advisory lock
    first: two concurrent writers reading the same head would otherwise compute
    the same `seq` and chain off the same `prev_hash`, producing a fork. The lock
    is transaction-scoped, so it releases on commit or rollback with no cleanup.

    The unique constraint on (tenant_id, seq) is the backstop if the lock is ever
    bypassed — belt and braces, because a forked chain is unrecoverable evidence.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :tenant)"),
        # hashtext gives a stable 32-bit int from the uuid; collisions between
        # tenants only cost a little contention, never correctness.
        {"ns": _LOCK_NAMESPACE, "tenant": _tenant_lock_key(tenant_id)},
    )

    head = (
        await session.execute(
            select(AuditEvent.seq, AuditEvent.hash)
            .where(AuditEvent.tenant_id == tenant_id)
            .order_by(AuditEvent.seq.desc())
            .limit(1)
        )
    ).first()

    seq = (head.seq + 1) if head else 1
    prev_hash = head.hash if head else GENESIS_HASH
    body = payload or {}

    entry = AuditEvent(
        tenant_id=tenant_id,
        seq=seq,
        actor_type=actor.type,
        actor_id=actor.id,
        actor_label=actor.label,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=body,
        ip_address=actor.ip,
        user_agent=actor.user_agent,
        prev_hash=prev_hash,
        hash=audit_hash(
            tenant_id=str(tenant_id), seq=seq, action=action, payload=body, prev_hash=prev_hash
        ),
    )
    session.add(entry)
    # Flush now rather than at commit: it surfaces a constraint violation here,
    # where the caller can still react, and it fixes ordering when one request
    # writes several entries.
    await session.flush()
    return entry


async def verify_chain(
    session: AsyncSession, *, tenant_id: uuid.UUID, limit: int | None = None
) -> ChainStatus:
    """Walk a tenant's chain and recompute every hash.

    Detects three kinds of tampering:
      * a modified row      — its own hash no longer matches its contents
      * a deleted row       — the next row's prev_hash no longer matches
      * a reordered row     — same failure as deletion, from the other side

    What it cannot detect is truncation of the newest entries with nothing after
    them. That is what periodic external anchoring (Phase 10) is for: sign the
    head hash on a schedule into WORM storage, and a missing tail becomes visible.
    """
    stmt = (
        select(AuditEvent)
        .where(AuditEvent.tenant_id == tenant_id)
        .order_by(AuditEvent.seq.asc())
    )
    if limit:
        stmt = stmt.limit(limit)

    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        return ChainStatus(ok=True, checked=0, head_seq=None, head_hash=None)

    expected_prev = GENESIS_HASH
    expected_seq = 1

    for row in rows:
        if row.seq != expected_seq:
            return ChainStatus(
                ok=False, checked=expected_seq - 1, head_seq=rows[-1].seq,
                head_hash=rows[-1].hash, first_broken_seq=row.seq,
                problem=f"sequence gap: expected {expected_seq}, found {row.seq}",
            )
        if row.prev_hash != expected_prev:
            return ChainStatus(
                ok=False, checked=expected_seq - 1, head_seq=rows[-1].seq,
                head_hash=rows[-1].hash, first_broken_seq=row.seq,
                problem="prev_hash does not match the previous entry — an entry was altered or removed",
            )
        recomputed = audit_hash(
            tenant_id=str(row.tenant_id), seq=row.seq, action=row.action,
            payload=row.payload, prev_hash=row.prev_hash,
        )
        if recomputed != row.hash:
            return ChainStatus(
                ok=False, checked=expected_seq - 1, head_seq=rows[-1].seq,
                head_hash=rows[-1].hash, first_broken_seq=row.seq,
                problem="hash does not match contents — this entry was modified",
            )
        expected_prev = row.hash
        expected_seq += 1

    return ChainStatus(
        ok=True, checked=len(rows), head_seq=rows[-1].seq, head_hash=rows[-1].hash
    )


def _tenant_lock_key(tenant_id: uuid.UUID) -> int:
    """Fold a UUID into the signed 32-bit int pg_advisory_xact_lock wants."""
    return (tenant_id.int % (2**31 - 1)) - (2**30)
