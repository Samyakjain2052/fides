"""The vendor register (§8(2)).

Most of these are about `concerns()`, which is the thing this module offers
instead of a risk score. The tests assert that each concern appears for the
right reason, that it explains what turns on it, and — importantly — that no
composite number is produced anywhere.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.errors import NotFound, ValidationProblem
from app.db.session import set_tenant_context
from app.models.audit import AuditAction, AuditEvent
from app.models.connection import Connection
from app.models.tenant import Tenant
from app.models.vendor import Vendor
from app.services import vendor_service
from app.services.audit_service import Actor
from app.services.vendor_service import VendorRefused

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


async def _vendor(session, tenant, *, name=None, **kw):
    return await vendor_service.create(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        name=name or f"Vendor {uuid.uuid4().hex[:6]}", **kw,
    )


async def _titles(session, vendor, tenant_row=None):
    return [
        c["title"]
        for c in await vendor_service.concerns(
            session, vendor=vendor, tenant=tenant_row
        )
    ]


# --------------------------------------------------------------------------- #
# The register
# --------------------------------------------------------------------------- #

async def test_a_vendor_starts_prospective(app_session_factory, tenant_a):
    """Recording that a vendor exists and deciding they may receive personal
    data are different acts — the same reason a connection starts unverified."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a, name="Mailchimp")
    assert v.status == "prospective"
    assert v.in_use is False


async def test_a_duplicate_name_is_refused_without_poisoning_the_transaction(
    app_session_factory, tenant_a
):
    """A savepoint, so the caller's other work survives.

    The trap that produced a 500 on duplicate retention policy names.
    """
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            await _vendor(session, tenant_a, name="Twilio")
            with pytest.raises(VendorRefused) as err:
                await _vendor(session, tenant_a, name="Twilio")
            # The transaction is still usable: this insert must succeed.
            survivor = await _vendor(session, tenant_a, name="Zoho")
            assert survivor.id is not None
    assert "already in the register" in str(err.value)


async def test_status_cannot_be_changed_through_the_generic_update(
    app_session_factory, tenant_a
):
    """A vendor should not become approved with nobody's name on it."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            with pytest.raises(ValidationProblem) as err:
                await vendor_service.update(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    vendor=v, status="approved",
                )
    assert "decision endpoint" in str(err.value)


async def test_refusing_a_vendor_requires_a_reason(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            with pytest.raises(ValidationProblem) as err:
                await vendor_service.decide(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    vendor=v, status="rejected",
                )
    assert "needs a reason" in str(err.value)


async def test_a_conditional_approval_requires_its_conditions(
    app_session_factory, tenant_a
):
    """Most real vendor relationships are conditional. A two-value model forces
    people to record the untrue one — but the conditions have to be written."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            with pytest.raises(ValidationProblem) as err:
                await vendor_service.decide(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    vendor=v, status="conditional",
                )
    assert "remains outstanding" in str(err.value)


async def test_the_database_also_refuses_a_reason_free_rejection(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            vendor_id = v.id

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        v = await session.scalar(select(Vendor).where(Vendor.id == vendor_id))
        v.status = "rejected"
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_approving_sets_the_review_clock(app_session_factory, tenant_a):
    """A vendor approved today should not still be approved on today's evidence
    in three years."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            await vendor_service.update(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, review_every_days=365,
            )
            await vendor_service.decide(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, status="approved",
            )
    assert v.next_review_at is not None
    assert v.next_review_at > datetime.now(UTC) + timedelta(days=360)


async def test_a_decision_is_audited_with_its_reason(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a, name="Refused Ltd")
            await vendor_service.decide(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, status="rejected",
                note="No DPA on offer and they would not name their "
                     "sub-processors.",
            )
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.VENDOR_DECIDED
                )
            )
        ).scalars().one()
    assert event.payload["to"] == "rejected"
    assert "sub-processors" in event.payload["note"]


async def test_a_signed_dpa_needs_its_date(app_session_factory, tenant_a):
    """Without one there is no telling whether it predates the processing."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            with pytest.raises(ValidationProblem) as err:
                await vendor_service.update(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    vendor=v, dpa_signed=True,
                )
    assert "needs its date" in str(err.value)


# --------------------------------------------------------------------------- #
# Concerns — the thing offered instead of a score
# --------------------------------------------------------------------------- #

async def test_no_score_is_produced_anywhere(app_session_factory, tenant_a):
    """The deliberate omission.

    A number per vendor is the output of a research operation, not of a form. If
    a future change adds one, this fails — which is the intent.
    """
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            await vendor_service.decide(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, status="approved",
            )
            out = vendor_service.as_dict(
                v,
                concern_list=await vendor_service.concerns(session, vendor=v),
            )
    assert "score" not in out
    assert "risk_score" not in out
    assert "privacy_score" not in out
    # What it offers instead: a count and a worst severity, both derived from
    # facts with named causes.
    assert isinstance(out["concern_count"], int)
    assert out["worst_severity"] in ("high", "medium", "low", None)
    assert all({"severity", "title", "why"} <= set(c) for c in out["concerns"])


async def test_an_in_use_vendor_with_no_dpa_is_flagged_high(
    app_session_factory, tenant_a
):
    """§8(2) makes them responsible whether or not a contract exists."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            await vendor_service.decide(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, status="approved",
            )
            found = await vendor_service.concerns(session, vendor=v)

    dpa = next(c for c in found if "No signed data processing" in c["title"])
    assert dpa["severity"] == "high"
    assert "§8(2)" in dpa["why"]


async def test_a_prospective_vendor_is_not_flagged_for_a_missing_dpa(
    app_session_factory, tenant_a
):
    """They are not receiving data yet. Flagging it would be noise, and noise is
    how a concern list gets ignored."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            titles = await _titles(session, v)
    assert not any("No signed data processing" in t for t in titles)


async def test_an_expired_dpa_is_flagged(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            await vendor_service.update(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, dpa_signed=True,
                dpa_signed_on=date.today() - timedelta(days=800),
                dpa_expires_on=date.today() - timedelta(days=30),
            )
            await vendor_service.decide(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, status="approved",
            )
            titles = await _titles(session, v)
    assert any("expired" in t for t in titles)
    assert v.dpa_expired is True


async def test_a_dpa_expiring_soon_is_flagged_before_it_lapses(
    app_session_factory, tenant_a
):
    """Renewal takes longer than people expect."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            await vendor_service.update(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, dpa_signed=True,
                dpa_signed_on=date.today() - timedelta(days=300),
                dpa_expires_on=date.today() + timedelta(days=20),
            )
            await vendor_service.decide(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, status="approved",
            )
            titles = await _titles(session, v)
    assert any("expires" in t for t in titles)
    assert v.dpa_expired is False


async def test_no_dsar_route_is_flagged_high(app_session_factory, tenant_a):
    """Without one the obligation is undischargeable, not merely slow."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            await vendor_service.decide(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, status="approved",
            )
            found = await vendor_service.concerns(session, vendor=v)
    route = next(c for c in found if "route for a rights request" in c["title"])
    assert route["severity"] == "high"
    assert "undischargeable" in route["why"]


async def test_a_vendor_slower_than_our_own_deadline_is_flagged(
    app_session_factory, tenant_a
):
    """The most useful comparison in the module.

    The statutory clock runs on the fiduciary, so a processor who takes as long
    as the whole window makes them late by default.
    """
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            tenant = await session.scalar(
                select(Tenant).where(Tenant.id == tenant_a["id"])
            )
            v = await _vendor(session, tenant_a)
            await vendor_service.update(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, dsar_contact="privacy@vendor.example",
                dsar_sla_days=tenant.dsar_sla_days + 15,
            )
            await vendor_service.decide(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, status="approved",
            )
            found = await vendor_service.concerns(
                session, vendor=v, tenant=tenant
            )
    slow = next(c for c in found if "turnaround" in c["title"])
    assert slow["severity"] == "high"
    assert "clock runs on you" in slow["why"]


async def test_a_vendor_faster_than_our_deadline_is_not_flagged_for_speed(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            tenant = await session.scalar(
                select(Tenant).where(Tenant.id == tenant_a["id"])
            )
            v = await _vendor(session, tenant_a)
            await vendor_service.update(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, dsar_contact="privacy@vendor.example",
                dsar_sla_days=max(1, tenant.dsar_sla_days - 10),
            )
            await vendor_service.decide(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, status="approved",
            )
            titles = await _titles(session, v, tenant)
    assert not any("turnaround" in t for t in titles)


async def test_transfers_with_no_location_are_flagged(app_session_factory, tenant_a):
    """§16 cannot be assessed without knowing where the data goes."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            await vendor_service.update(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, transfers_outside_india=True,
            )
            found = await vendor_service.concerns(session, vendor=v)
    transfer = next(c for c in found if "Transfers outside India" in c["title"])
    assert "§16" in transfer["why"]


async def test_an_overdue_review_is_flagged(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            await vendor_service.decide(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, status="approved",
            )
            v.next_review_at = datetime.now(UTC) - timedelta(days=5)
            titles = await _titles(session, v)
    assert any("Review overdue" in t for t in titles)
    assert v.review_overdue is True


async def test_a_clean_vendor_has_no_concerns(app_session_factory, tenant_a):
    """The list has to be able to be empty, or it is decoration."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            tenant = await session.scalar(
                select(Tenant).where(Tenant.id == tenant_a["id"])
            )
            v = await _vendor(session, tenant_a, owner_user_id=tenant_a["admin_id"])
            await vendor_service.update(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v,
                dpa_signed=True,
                dpa_signed_on=date.today() - timedelta(days=100),
                dpa_expires_on=date.today() + timedelta(days=500),
                dsar_contact="privacy@vendor.example",
                dsar_sla_days=max(1, tenant.dsar_sla_days - 15),
                breach_notice_hours=24,
                data_location="AWS ap-south-1 (Mumbai)",
                transfers_outside_india=False,
                subprocessors=[{"name": "AWS", "purpose": "hosting"}],
                review_every_days=365,
            )
            await vendor_service.decide(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, status="approved",
            )
            found = await vendor_service.concerns(
                session, vendor=v, tenant=tenant
            )
    assert found == [], [c["title"] for c in found]


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #

async def test_a_document_needs_a_link_or_a_file(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            with pytest.raises(ValidationProblem) as err:
                await vendor_service.add_document(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    vendor=v, kind="privacy_policy", title="Their policy",
                )
    assert "note about nothing" in str(err.value)


async def test_the_fingerprint_ignores_reformatting(app_session_factory, tenant_a):
    """A checker that cries wolf on whitespace gets ignored, which is worse than
    not having one."""
    first = vendor_service.content_fingerprint("We  collect\n\nyour   data.")
    second = vendor_service.content_fingerprint("We collect your data.")
    assert first == second


async def test_the_fingerprint_notices_a_substantive_edit(app_session_factory):
    a = vendor_service.content_fingerprint("We do not sell your data.")
    b = vendor_service.content_fingerprint("We may sell your data.")
    assert a != b


async def test_the_first_reading_is_not_a_change(app_session_factory, tenant_a):
    """Otherwise every new document arrives already 'changed'."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            d = await vendor_service.add_document(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, kind="privacy_policy", title="Policy",
                url="https://vendor.example/privacy",
            )
            changed = await vendor_service.note_content_changed(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                document=d, content="Original text.",
            )
    assert changed is False
    assert d.changed_since_review is False
    assert d.content_hash


async def test_a_changed_policy_is_flagged_and_audited(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            d = await vendor_service.add_document(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, kind="privacy_policy", title="Policy",
                url="https://vendor.example/privacy",
            )
            await vendor_service.note_content_changed(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                document=d, content="We do not share data with anyone.",
            )
            changed = await vendor_service.note_content_changed(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                document=d,
                content="We share data with our advertising partners.",
            )
            assert changed is True
            titles = await _titles(session, v)

    assert any("changed since anybody read them" in t for t in titles)

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.VENDOR_DOCUMENT_CHANGED
                )
            )
        ).scalars().all()
    assert len(events) == 1


async def test_recording_a_review_clears_the_changed_flag(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            d = await vendor_service.add_document(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                vendor=v, kind="privacy_policy", title="Policy",
                url="https://vendor.example/privacy",
            )
            await vendor_service.note_content_changed(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                document=d, content="One.",
            )
            await vendor_service.note_content_changed(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                document=d, content="Two — quite different.",
            )
            assert d.changed_since_review is True
            await vendor_service.record_review(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                document=d, content="Two — quite different.",
            )
    assert d.changed_since_review is False
    assert d.last_seen_at is not None


# --------------------------------------------------------------------------- #
# The join to the data map
# --------------------------------------------------------------------------- #

async def test_a_vendor_can_be_linked_to_a_connection(app_session_factory, tenant_a):
    """"This vendor is retired — which of our systems does that affect?" """
    from app.core.crypto import seal

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            connection = Connection(
                tenant_id=tenant_a["id"], connector_id="postgresql",
                label="crm", status="connected",
                config_sealed=seal({"password": "x"}),
                config_public={"host": "db"}, hints={},
            )
            session.add(connection)
            await session.flush()

            v = await _vendor(session, tenant_a)
            await vendor_service.link_system(
                session, tenant_id=tenant_a["id"], vendor=v,
                connection_id=connection.id,
            )
            systems = await vendor_service.systems_for(session, vendor_id=v.id)
    assert [s.label for s in systems] == ["crm"]


async def test_linking_twice_is_idempotent(app_session_factory, tenant_a):
    from app.core.crypto import seal

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            connection = Connection(
                tenant_id=tenant_a["id"], connector_id="mysql", label="wh",
                status="connected", config_sealed=seal({"password": "x"}),
                config_public={"host": "db"}, hints={},
            )
            session.add(connection)
            await session.flush()
            v = await _vendor(session, tenant_a)
            await vendor_service.link_system(
                session, tenant_id=tenant_a["id"], vendor=v,
                connection_id=connection.id,
            )
            await vendor_service.link_system(
                session, tenant_id=tenant_a["id"], vendor=v,
                connection_id=connection.id,
            )
            systems = await vendor_service.systems_for(session, vendor_id=v.id)
    assert len(systems) == 1


async def test_linking_an_unknown_connection_is_refused(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a)
            with pytest.raises(NotFound):
                await vendor_service.link_system(
                    session, tenant_id=tenant_a["id"], vendor=v,
                    connection_id=uuid.uuid4(),
                )


async def test_vendors_are_isolated_between_tenants(
    app_session_factory, tenant_a, tenant_b
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            v = await _vendor(session, tenant_a, name="Private Ltd")
            vendor_id = v.id

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_b["id"])
        assert await vendor_service.list_all(session) == []
        with pytest.raises(NotFound):
            await vendor_service.get(session, vendor_id=vendor_id)


async def test_the_same_vendor_name_is_allowed_in_two_workspaces(
    app_session_factory, tenant_a, tenant_b
):
    """The uniqueness constraint is per tenant. Two customers both using
    Mailchimp is the normal case."""
    for tenant in (tenant_a, tenant_b):
        async with app_session_factory() as session:
            async with session.begin():
                await set_tenant_context(session, tenant["id"])
                v = await _vendor(session, tenant, name="Mailchimp")
                assert v.name == "Mailchimp"
