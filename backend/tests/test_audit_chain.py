"""
Audit chain tests — the product's core claim, verified rather than asserted.

Four things must hold:
  1. a clean chain verifies
  2. modifying an entry is detected
  3. deleting an entry is detected
  4. the application cannot modify or delete an entry in the first place
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from app.db.session import set_tenant_context
from app.models.audit import AuditEvent
from app.services import audit_service
from app.services.audit_service import Actor

GENESIS = "0" * 64


async def _write(session, tenant_id, n: int) -> None:
    for i in range(n):
        await audit_service.record(
            session,
            tenant_id=tenant_id,
            actor=Actor.system("test"),
            action="consent.granted",
            payload={"i": i, "purpose": f"p{i}"},
        )


async def test_clean_chain_verifies(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            await _write(session, tenant_a["id"], 5)

        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            status = await audit_service.verify_chain(session, tenant_id=tenant_a["id"])

    assert status.ok, status.problem
    # 2 bootstrap entries from tenant creation + 5 written here.
    assert status.checked == 7
    assert status.head_seq == 7


async def test_chain_starts_at_genesis_and_increments(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            rows = list(
                (
                    await session.execute(select(AuditEvent).order_by(AuditEvent.seq))
                ).scalars()
            )

    assert rows[0].seq == 1
    assert rows[0].prev_hash == GENESIS
    for prev, cur in zip(rows, rows[1:], strict=False):
        assert cur.seq == prev.seq + 1
        assert cur.prev_hash == prev.hash, "chain link broken"


async def test_modified_entry_is_detected(app_session_factory, owner_engine, tenant_a):
    """Tamper with a row's contents and verification must fail.

    The UPDATE is performed as the OWNER with the trigger disabled, because the
    application genuinely cannot do this — we are simulating an attacker with
    direct database access, which is the threat the hash chain exists for.
    """
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            await _write(session, tenant_a["id"], 4)

    async with owner_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete"))
        # The tables are FORCE ROW LEVEL SECURITY, so even the owner needs the
        # tenant variable set — without it this UPDATE matches zero rows and the
        # test silently tampers with nothing. (That is RLS doing its job, and it
        # is why this line exists.)
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(tenant_a["id"])}
        )
        await conn.execute(
            text(
                "UPDATE audit_events SET payload = '{\"i\": 999, \"purpose\": \"forged\"}'::jsonb "
                "WHERE tenant_id = :t AND seq = 3"
            ),
            {"t": tenant_a["id"]},
        )
        await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_no_update_delete"))

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            status = await audit_service.verify_chain(session, tenant_id=tenant_a["id"])

    assert not status.ok, "a modified entry went undetected — the chain is worthless"
    assert status.first_broken_seq == 3
    assert "modified" in (status.problem or "")


async def test_deleted_entry_is_detected(app_session_factory, owner_engine, tenant_a):
    """Remove a row and the gap plus the broken link must both show up."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            await _write(session, tenant_a["id"], 4)

    async with owner_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete"))
        # FORCE RLS applies to the owner too — see the note in the test above.
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(tenant_a["id"])}
        )
        await conn.execute(
            text("DELETE FROM audit_events WHERE tenant_id = :t AND seq = 4"),
            {"t": tenant_a["id"]},
        )
        await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_no_update_delete"))

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            status = await audit_service.verify_chain(session, tenant_id=tenant_a["id"])

    assert not status.ok, "a deleted entry went undetected"
    assert status.first_broken_seq == 5


async def test_application_cannot_update_audit_rows(app_session_factory, tenant_a):
    """Defence in depth: the app role has no UPDATE grant, and a trigger raises."""
    async with app_session_factory() as session:
        with pytest.raises(Exception) as exc:
            async with session.begin():
                await set_tenant_context(session, tenant_a["id"])
                await session.execute(
                    text("UPDATE audit_events SET action = 'tampered' WHERE tenant_id = :t"),
                    {"t": tenant_a["id"]},
                )

    msg = str(exc.value).lower()
    assert "append-only" in msg or "permission denied" in msg or "denied" in msg, (
        f"expected the write to be refused, got: {exc.value}"
    )


async def test_application_cannot_delete_audit_rows(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        with pytest.raises(Exception) as exc:
            async with session.begin():
                await set_tenant_context(session, tenant_a["id"])
                await session.execute(
                    text("DELETE FROM audit_events WHERE tenant_id = :t"), {"t": tenant_a["id"]}
                )

    msg = str(exc.value).lower()
    assert "append-only" in msg or "permission denied" in msg or "denied" in msg, (
        f"expected the delete to be refused, got: {exc.value}"
    )


async def test_two_tenants_keep_independent_chains(app_session_factory, tenant_a, tenant_b):
    """Each tenant's seq starts at 1 and its chain is its own.

    A shared global sequence would leak volume — tenant B could infer how much
    activity tenant A has from the gaps in its own numbering.
    """
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            await _write(session, tenant_a["id"], 3)
        async with session.begin():
            await set_tenant_context(session, tenant_b["id"])
            await _write(session, tenant_b["id"], 2)

        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            a = await audit_service.verify_chain(session, tenant_id=tenant_a["id"])
        async with session.begin():
            await set_tenant_context(session, tenant_b["id"])
            b = await audit_service.verify_chain(session, tenant_id=tenant_b["id"])

    assert a.ok and b.ok
    assert a.head_seq == 5   # 2 bootstrap + 3
    assert b.head_seq == 4   # 2 bootstrap + 2
