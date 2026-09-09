"""Per-system work on a rights request: fan-out, ownership, honest closure.

The point of this table is that a negative result is a finding. Most of these
tests are about refusing to let an item close without saying what was concluded,
because that is the difference between "we searched payroll and it held nothing"
and "nobody looked at payroll" — which a scan expresses identically.
"""

from __future__ import annotations

import base64
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.errors import NotFound, ValidationProblem
from app.db.session import set_tenant_context
from app.models.audit import AuditAction, AuditEvent
from app.models.connection import Connection
from app.models.consent import DataPrincipal
from app.models.dsar_action_item import DsarActionItem
from app.services import action_item_service, dsar_service
from app.services.action_item_service import ActionItemRefused
from app.services.audit_service import Actor

TEST_KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("DS_CREDENTIAL_ENCRYPTION_KEY", TEST_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _actor(tenant: dict) -> Actor:
    return Actor(type="user", id=tenant["admin_id"], label="dpo@test")


async def _connection(
    session, tenant, *, connector_id="postgresql", label="billing",
    status="connected", owner=None,
):
    from app.core.crypto import seal

    row = Connection(
        tenant_id=tenant["id"],
        connector_id=connector_id,
        label=label,
        status=status,
        config_sealed=seal({"password": "x"}),
        config_public={"host": "db.internal"},
        hints={},
        owner_user_id=owner,
    )
    session.add(row)
    await session.flush()
    return row


async def _request(session, tenant, *, type_="erasure"):
    principal = DataPrincipal(
        tenant_id=tenant["id"],
        external_id=f"c-{uuid.uuid4().hex[:8]}",
        email="asha@example.com",
    )
    session.add(principal)
    await session.flush()
    return await dsar_service.submit(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        principal_id=principal.id, type=type_,
    )


# --------------------------------------------------------------------------- #
# Fan-out
# --------------------------------------------------------------------------- #

async def test_fan_out_creates_one_item_per_connected_system(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            await _connection(session, tenant_a, label="billing")
            await _connection(session, tenant_a, label="crm",
                              connector_id="mysql")
            request = await _request(session, tenant_a)
            created = await action_item_service.fan_out(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=request,
            )
    assert len(created) == 2
    assert {i.system_label for i in created} == {
        "PostgreSQL · billing", "MySQL · crm",
    }


async def test_an_unverified_connection_gets_no_item(app_session_factory, tenant_a):
    """An item nobody can do would make the queue look busy.

    A connection whose credentials have never worked is a Connections problem,
    and it already alerts. Creating work against it here would hide that.
    """
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            await _connection(session, tenant_a, label="never-worked",
                              status="unverified")
            request = await _request(session, tenant_a)
            created = await action_item_service.fan_out(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=request,
            )
    assert created == []


async def test_fan_out_is_idempotent(app_session_factory, tenant_a):
    """Re-running after a connection is added creates only what is missing."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            await _connection(session, tenant_a, label="billing")
            request = await _request(session, tenant_a)
            first = await action_item_service.fan_out(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=request,
            )
            request_id = request.id
    assert len(first) == 1

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            await _connection(session, tenant_a, label="crm",
                              connector_id="mysql")
            request = await dsar_service.get(session, tenant_a["id"], request_id)
            second = await action_item_service.fan_out(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=request,
            )
    assert len(second) == 1, "only the new system should produce an item"

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        items = await action_item_service.for_request(
            session, request_id=request_id
        )
    assert len(items) == 2


async def test_a_re_run_does_not_disturb_claimed_work(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            await _connection(session, tenant_a, label="billing")
            request = await _request(session, tenant_a)
            [item] = await action_item_service.fan_out(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=request,
            )
            await action_item_service.claim(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                item=item, user_id=tenant_a["admin_id"],
            )
            request_id, item_id = request.id, item.id

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            request = await dsar_service.get(session, tenant_a["id"], request_id)
            await action_item_service.fan_out(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=request,
            )

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        item = await session.scalar(
            select(DsarActionItem).where(DsarActionItem.id == item_id)
        )
    assert item.status == "claimed"
    assert item.assignee_user_id == tenant_a["admin_id"]


async def test_an_item_inherits_the_data_store_owner(app_session_factory, tenant_a):
    """This is what owners are for: work arrives assigned, or visibly not."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            await _connection(session, tenant_a, label="owned",
                              owner=tenant_a["admin_id"])
            await _connection(session, tenant_a, label="orphan",
                              connector_id="mysql")
            request = await _request(session, tenant_a)
            created = await action_item_service.fan_out(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=request,
            )

    by_label = {i.system_label: i for i in created}
    assert by_label["PostgreSQL · owned"].assignee_user_id == tenant_a["admin_id"]
    assert by_label["MySQL · orphan"].assignee_user_id is None


async def test_the_fan_out_audit_counts_the_unassigned(app_session_factory, tenant_a):
    """"Which of these has nobody" is the question a DPO needs answered."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            # `zoho_crm` is not a live connector, so the item needs a human and
            # has no owner — the case worth counting.
            await _connection(session, tenant_a, connector_id="zoho_crm",
                              label="crm")
            request = await _request(session, tenant_a)
            await action_item_service.fan_out(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=request,
            )

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.DSAR_ACTION_ITEMS_CREATED
                )
            )
        ).scalars().one()
    assert event.payload["unassigned"] == 1


async def test_a_manual_item_can_be_added_for_an_unreachable_system(
    app_session_factory, tenant_a
):
    """Most systems in most organisations. Tracking only the ones with
    credentials is tracking the easy part."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            request = await _request(session, tenant_a)
            item = await action_item_service.add_manual(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=request, system_label="Payroll bureau (Sharma & Co)",
            )
    assert item.connection_id is None
    assert item.automated is False


async def test_several_manual_items_can_coexist(app_session_factory, tenant_a):
    """The unique constraint is on (request, connection), and NULL connection
    ids do not collide — which is what makes this possible."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            request = await _request(session, tenant_a)
            for label in ("Payroll bureau", "Tape archive", "Ops spreadsheet"):
                await action_item_service.add_manual(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    request=request, system_label=label,
                )
            items = await action_item_service.for_request(
                session, request_id=request.id
            )
    assert len(items) == 3


# --------------------------------------------------------------------------- #
# Closing honestly — the reason this table exists
# --------------------------------------------------------------------------- #

async def _one_item(factory, tenant, *, type_="erasure"):
    async with factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant["id"])
            await _connection(session, tenant, label="billing")
            request = await _request(session, tenant, type_=type_)
            [item] = await action_item_service.fan_out(
                session, tenant_id=tenant["id"], actor=_actor(tenant),
                request=request,
            )
            return request.id, item.id


async def test_closing_requires_an_attestation(app_session_factory, tenant_a):
    """A dropdown value records that a button was pressed."""
    request_id, item_id = await _one_item(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            request = await dsar_service.get(session, tenant_a["id"], request_id)
            item = await action_item_service.get(session, item_id=item_id)
            with pytest.raises(ValidationProblem) as err:
                await action_item_service.close(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    request=request, item=item, user_id=tenant_a["admin_id"],
                    outcome="no_records_matched", attestation="   ",
                )
    assert "in a sentence" in str(err.value)


async def test_a_nil_result_is_recorded_as_a_finding(app_session_factory, tenant_a):
    """The whole point. "Searched, nothing matched" is a fact with a name on it."""
    request_id, item_id = await _one_item(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            request = await dsar_service.get(session, tenant_a["id"], request_id)
            item = await action_item_service.get(session, item_id=item_id)
            await action_item_service.close(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=request, item=item, user_id=tenant_a["admin_id"],
                outcome="no_records_matched",
                attestation="Searched by email and phone across all 14 tables; "
                            "no rows matched.",
            )

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        item = await session.scalar(
            select(DsarActionItem).where(DsarActionItem.id == item_id)
        )
        assert item.status == "completed"
        assert item.outcome == "no_records_matched"
        assert item.completed_by == tenant_a["admin_id"]
        assert item.completed_at is not None

        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.DSAR_ACTION_ITEM_CLOSED
                )
            )
        ).scalars().one()
    # The attestation is the evidence, so it is in the chain.
    assert "no rows matched" in event.payload["attestation"]


async def test_a_contradictory_close_is_refused(app_session_factory, tenant_a):
    """"Nothing matched" and "6 records found" cannot both be true."""
    request_id, item_id = await _one_item(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            request = await dsar_service.get(session, tenant_a["id"], request_id)
            item = await action_item_service.get(session, item_id=item_id)
            with pytest.raises(ValidationProblem) as err:
                await action_item_service.close(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    request=request, item=item, user_id=tenant_a["admin_id"],
                    outcome="no_records_matched", attestation="nothing here",
                    records_found=6,
                )
    assert "One of those is wrong" in str(err.value)


async def test_retaining_needs_a_stated_lawful_ground(app_session_factory, tenant_a):
    """Keeping somebody's data against their erasure request is a legal claim."""
    request_id, item_id = await _one_item(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            request = await dsar_service.get(session, tenant_a["id"], request_id)
            item = await action_item_service.get(session, item_id=item_id)
            with pytest.raises(ValidationProblem) as err:
                await action_item_service.close(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    request=request, item=item, user_id=tenant_a["admin_id"],
                    outcome="retained", attestation="keeping the invoices",
                )
    assert "lawful ground" in str(err.value)


async def test_retaining_with_a_ground_is_allowed(app_session_factory, tenant_a):
    request_id, item_id = await _one_item(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            request = await dsar_service.get(session, tenant_a["id"], request_id)
            item = await action_item_service.get(session, item_id=item_id)
            await action_item_service.close(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=request, item=item, user_id=tenant_a["admin_id"],
                outcome="retained", records_found=4,
                attestation="4 invoices retained; the person was removed from "
                            "the contact fields.",
                basis="Section 128 Companies Act 2013 — books of account must be "
                      "kept for 8 years.",
            )
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        item = await session.scalar(
            select(DsarActionItem).where(DsarActionItem.id == item_id)
        )
    assert item.outcome == "retained"
    assert "Companies Act" in item.skip_reason


async def test_the_database_refuses_a_completed_item_with_no_outcome(
    app_session_factory, tenant_a
):
    """A CHECK, not service politeness.

    No future caller can produce the silence this table exists to prevent by
    forgetting to set a field.
    """
    request_id, item_id = await _one_item(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        item = await session.scalar(
            select(DsarActionItem).where(DsarActionItem.id == item_id)
        )
        item.status = "completed"  # and nothing else
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_skipping_requires_a_reason(app_session_factory, tenant_a):
    request_id, item_id = await _one_item(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            request = await dsar_service.get(session, tenant_a["id"], request_id)
            item = await action_item_service.get(session, item_id=item_id)
            with pytest.raises(ValidationProblem):
                await action_item_service.skip(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    request=request, item=item, reason="",
                )


async def test_closing_a_closed_item_is_refused(app_session_factory, tenant_a):
    request_id, item_id = await _one_item(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            request = await dsar_service.get(session, tenant_a["id"], request_id)
            item = await action_item_service.get(session, item_id=item_id)
            await action_item_service.close(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=request, item=item, user_id=tenant_a["admin_id"],
                outcome="erased", attestation="masked 3 rows", records_found=3,
            )
            with pytest.raises(ActionItemRefused) as err:
                await action_item_service.close(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    request=request, item=item, user_id=tenant_a["admin_id"],
                    outcome="erased", attestation="again",
                )
    assert "Reopen it first" in str(err.value)


async def test_reopening_clears_the_conclusion_and_keeps_it_in_the_chain(
    app_session_factory, tenant_a
):
    """A completed_at on an open item is how a report counts work twice."""
    request_id, item_id = await _one_item(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            request = await dsar_service.get(session, tenant_a["id"], request_id)
            item = await action_item_service.get(session, item_id=item_id)
            await action_item_service.close(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=request, item=item, user_id=tenant_a["admin_id"],
                outcome="no_records_matched",
                attestation="nothing found on first pass",
            )
            await action_item_service.reopen(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                item=item, reason="searched the wrong schema",
            )

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        item = await session.scalar(
            select(DsarActionItem).where(DsarActionItem.id == item_id)
        )
        assert item.outcome is None
        assert item.attestation is None
        assert item.completed_at is None
        assert item.is_open

        # The superseded claim survives in the audit chain.
        closed = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.DSAR_ACTION_ITEM_CLOSED
                )
            )
        ).scalars().all()
    assert len(closed) == 1
    assert "nothing found on first pass" in closed[0].payload["attestation"]


# --------------------------------------------------------------------------- #
# Assignment
# --------------------------------------------------------------------------- #

async def test_assigning_to_a_revoked_user_is_refused(app_session_factory, tenant_a):
    """Work assigned to somebody who cannot sign in is work nobody will do.

    Revokes a second account rather than the admin: a CHECK constraint keeps a
    workspace from losing its last active admin, which is a guard worth leaving
    alone.
    """
    from app.core.security import hash_password
    from app.models.user import User

    request_id, item_id = await _one_item(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            leaver = User(
                tenant_id=tenant_a["id"],
                email="leaver@tenant-a.example.com",
                full_name="Someone Who Left",
                role="grievance_officer",
                password_hash=hash_password("correct-horse-battery-staple"),
                is_active=False,
            )
            session.add(leaver)
            await session.flush()
            leaver_id = leaver.id

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            item = await action_item_service.get(session, item_id=item_id)
            with pytest.raises(ActionItemRefused) as err:
                await action_item_service.assign(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    item=item, assignee_user_id=leaver_id,
                )
    assert "active account" in str(err.value)


async def test_a_user_from_another_workspace_cannot_be_assigned(
    app_session_factory, tenant_a, tenant_b
):
    """RLS means they are simply not there, and the refusal says nothing about
    whether that account exists elsewhere."""
    request_id, item_id = await _one_item(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            item = await action_item_service.get(session, item_id=item_id)
            with pytest.raises(ActionItemRefused):
                await action_item_service.assign(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    item=item, assignee_user_id=tenant_b["admin_id"],
                )


async def test_unassigning_returns_an_item_to_pending(app_session_factory, tenant_a):
    request_id, item_id = await _one_item(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            item = await action_item_service.get(session, item_id=item_id)
            await action_item_service.claim(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                item=item, user_id=tenant_a["admin_id"],
            )
            await action_item_service.assign(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                item=item, assignee_user_id=None,
            )
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        item = await session.scalar(
            select(DsarActionItem).where(DsarActionItem.id == item_id)
        )
    assert item.status == "pending"
    assert item.assignee_user_id is None
    assert item.claimed_at is None


# --------------------------------------------------------------------------- #
# Third parties — §8(2)
# --------------------------------------------------------------------------- #

async def test_third_parties_accumulate_with_their_own_timestamps(
    app_session_factory, tenant_a
):
    """One `notified_at` cannot express "the vendor in March, their
    sub-processor in April"."""
    request_id, item_id = await _one_item(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            item = await action_item_service.get(session, item_id=item_id)
            await action_item_service.record_third_party(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                item=item, address="privacy@vendor.example",
                note="asked to delete under the DPA",
            )
            await action_item_service.record_third_party(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                item=item, address="dpo@subprocessor.example",
            )

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        item = await session.scalar(
            select(DsarActionItem).where(DsarActionItem.id == item_id)
        )
    assert len(item.third_parties_notified) == 2
    assert all(e["notified_at"] for e in item.third_parties_notified)
    assert item.third_parties_notified[0]["note"] == "asked to delete under the DPA"


async def test_recording_a_third_party_is_audited(app_session_factory, tenant_a):
    request_id, item_id = await _one_item(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            item = await action_item_service.get(session, item_id=item_id)
            await action_item_service.record_third_party(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                item=item, address="privacy@vendor.example",
            )
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.DSAR_THIRD_PARTY_NOTIFIED
                )
            )
        ).scalars().all()
    assert len(events) == 1
    assert events[0].payload["address"] == "privacy@vendor.example"


# --------------------------------------------------------------------------- #
# Summary, and the line it does not cross
# --------------------------------------------------------------------------- #

async def test_the_summary_counts_what_a_dpo_needs(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            await _connection(session, tenant_a, label="a")
            await _connection(session, tenant_a, label="b", connector_id="mysql")
            request = await _request(session, tenant_a)
            items = await action_item_service.fan_out(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=request,
            )
            await action_item_service.close(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=request, item=items[0], user_id=tenant_a["admin_id"],
                outcome="erased", attestation="masked 2 rows", records_found=2,
            )
            all_items = await action_item_service.for_request(
                session, request_id=request.id
            )
            summary = action_item_service.summarise(all_items)

    assert summary["total"] == 2
    assert summary["completed"] == 1
    assert summary["open"] == 1
    assert summary["records_found"] == 2
    assert summary["all_closed"] is False


async def test_closing_every_item_does_not_complete_the_request(
    app_session_factory, tenant_a
):
    """Whether everything in scope was reached is a judgement a human signs.

    The same line `data_map_service.erase` already refuses to cross.
    """
    request_id, item_id = await _one_item(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            request = await dsar_service.get(session, tenant_a["id"], request_id)
            item = await action_item_service.get(session, item_id=item_id)
            await action_item_service.close(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=request, item=item, user_id=tenant_a["admin_id"],
                outcome="erased", attestation="done", records_found=1,
            )

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        request = await dsar_service.get(session, tenant_a["id"], request_id)
        items = await action_item_service.for_request(session, request_id=request_id)
    assert action_item_service.summarise(items)["all_closed"] is True
    assert request.status != "completed", "the request must still be closed by a human"


async def test_items_are_isolated_between_tenants(
    app_session_factory, tenant_a, tenant_b
):
    request_id, item_id = await _one_item(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_b["id"])
        assert await action_item_service.for_request(
            session, request_id=request_id
        ) == []
        with pytest.raises(NotFound):
            await action_item_service.get(session, item_id=item_id)
