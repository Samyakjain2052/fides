"""Outbound alerts to fiduciaries and processors.

This is the feature that makes a withdrawal take effect rather than merely be
recorded, so the tests are weighted towards the ways that promise could quietly
fail: an alert that never fires, a withdrawal that breaks because a subscriber
is down, a signature somebody can forge or replay, and a delivery log that
claims more than it knows.

The delivery tests inject an `httpx.AsyncClient` built on `MockTransport`. That
is why `attempt()` takes a client rather than making its own — a send path that
cannot be given a fake is a send path that is either untested or tested against
somebody's real server.
"""

from __future__ import annotations

import base64
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.db.session import set_tenant_context
from app.models.audit import AuditAction, AuditEvent
from app.models.consent import Consent, DataPrincipal, Purpose
from app.models.webhook import FAILURE_BUDGET, WebhookDelivery, WebhookEndpoint
from app.services import consent_service, webhook_service
from app.services.audit_service import Actor

TEST_KEY = base64.b64encode(b"w" * 32).decode()


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    """An endpoint's signing secret is sealed, so the suite needs a key."""
    from app.core.config import get_settings

    monkeypatch.setenv("DS_CREDENTIAL_ENCRYPTION_KEY", TEST_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _actor(tenant: dict) -> Actor:
    return Actor(type="user", id=tenant["admin_id"], label="dpo@test")


@asynccontextmanager
async def scoped(factory, tenant_id):
    async with factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_id)
        try:
            yield session
        finally:
            if session.in_transaction():
                await session.rollback()


#: A sentinel, so `events=[]` reaches the service instead of being replaced by
#: the default. `events or [...]` would make the "subscribed to nothing" test
#: silently assert the opposite of what it says.
_DEFAULT_EVENTS = object()


async def _endpoint(
    session, tenant, *, events=_DEFAULT_EVENTS, url="https://example.com/hook"
):
    endpoint, secret = await webhook_service.create_endpoint(
        session,
        tenant_id=tenant["id"],
        actor=_actor(tenant),
        url=url,
        label="Processor",
        events=["consent.withdrawn"] if events is _DEFAULT_EVENTS else events,
    )
    return endpoint, secret


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# Signing
# --------------------------------------------------------------------------- #

def test_a_signature_verifies_against_the_body_it_was_made_for():
    body = b'{"event":"consent.withdrawn"}'
    ts = int(datetime.now(UTC).timestamp())
    header = webhook_service.sign("sekrit", body=body, timestamp=ts)
    assert webhook_service.verify("sekrit", body=body, header=header)


def test_a_changed_body_fails_verification():
    """The point of signing at all.

    Without this, "stop processing for person X" is an instruction anybody who
    can reach the customer's endpoint may rewrite in flight.
    """
    ts = int(datetime.now(UTC).timestamp())
    header = webhook_service.sign("sekrit", body=b'{"purpose":"marketing"}', timestamp=ts)
    assert not webhook_service.verify(
        "sekrit", body=b'{"purpose":"everything"}', header=header
    )


def test_a_signature_from_a_different_secret_fails():
    body = b"{}"
    ts = int(datetime.now(UTC).timestamp())
    header = webhook_service.sign("theirs", body=body, timestamp=ts)
    assert not webhook_service.verify("ours", body=body, header=header)


def test_an_old_signature_is_refused_even_though_the_digest_is_right():
    """Replay, which is the attack the timestamp exists to stop.

    A captured `consent.granted` replayed after the person withdrew would tell a
    processor to resume something that had lawfully stopped. The digest is
    perfectly valid; the age is what makes it unacceptable.
    """
    body = b'{"event":"consent.granted"}'
    stale = int((datetime.now(UTC) - timedelta(hours=2)).timestamp())
    header = webhook_service.sign("sekrit", body=body, timestamp=stale)

    assert not webhook_service.verify("sekrit", body=body, header=header)
    # ...and it really is only the age: widen the window and the same header passes.
    assert webhook_service.verify("sekrit", body=body, header=header, tolerance=10_000)


def test_the_timestamp_cannot_be_edited_without_breaking_the_digest():
    """It is signed material, not a hint alongside it.

    A `t=` a caller could rewrite would make the replay window unbounded, which
    is the whole thing it exists to bound.
    """
    body = b"{}"
    stale = int((datetime.now(UTC) - timedelta(hours=2)).timestamp())
    header = webhook_service.sign("sekrit", body=body, timestamp=stale)
    digest = header.split("v1=")[1]

    forged = f"t={int(datetime.now(UTC).timestamp())},v1={digest}"
    assert not webhook_service.verify("sekrit", body=body, header=forged)


def test_a_malformed_signature_header_is_refused_not_crashed():
    for header in ("", "garbage", "t=notanumber,v1=abc", "v1=abc"):
        assert not webhook_service.verify("sekrit", body=b"{}", header=header)


# --------------------------------------------------------------------------- #
# What may be registered
# --------------------------------------------------------------------------- #

async def test_a_plain_http_endpoint_is_refused(app_session_factory, tenant_a):
    """These bodies carry a person's identifier and an instruction about their
    data. http would put both on the wire in clear."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        with pytest.raises(webhook_service.EndpointUnusable) as exc:
            await _endpoint(session, tenant_a, url="http://example.com/hook")
    assert "https" in str(exc.value)


async def test_an_unknown_event_is_refused(app_session_factory, tenant_a):
    """The set is closed. A typo would otherwise create an endpoint that looks
    subscribed and receives nothing."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        with pytest.raises(Exception) as exc:
            await _endpoint(session, tenant_a, events=["consent.withdrawnn"])
    assert "consent.withdrawnn" in str(exc.value)


async def test_an_endpoint_subscribed_to_nothing_is_refused(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        with pytest.raises(Exception):
            await _endpoint(session, tenant_a, events=[])


async def test_a_private_address_is_refused(app_session_factory, tenant_a, monkeypatch):
    """Same SSRF reasoning as connections, and it matters more here: this one is
    reached on a timer by a background worker, with no human watching."""
    from app.core.config import get_settings

    monkeypatch.setenv("DS_CONNECTOR_ALLOW_PRIVATE_HOSTS", "false")
    get_settings.cache_clear()

    async with scoped(app_session_factory, tenant_a["id"]) as session:
        with pytest.raises(webhook_service.EndpointUnusable):
            await _endpoint(session, tenant_a, url="https://169.254.169.254/latest")


# --------------------------------------------------------------------------- #
# The secret
# --------------------------------------------------------------------------- #

async def test_the_secret_is_returned_once_and_never_serialised_again(
    app_session_factory, tenant_a
):
    """`endpoint_out` cannot express it, which is stronger than a router that
    remembers to leave it out."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        endpoint, secret = await _endpoint(session, tenant_a)
        out = webhook_service.endpoint_out(endpoint)

    assert secret
    assert "secret" not in json.dumps(out).replace("secret_hint", "").replace(
        "secret_rotated_at", ""
    )
    assert secret not in json.dumps(out)


async def test_rotation_invalidates_the_previous_secret(app_session_factory, tenant_a):
    """No overlap window: rotation usually happens BECAUSE one leaked."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        endpoint, old = await _endpoint(session, tenant_a)
        new = await webhook_service.rotate_secret(
            session,
            tenant_id=tenant_a["id"],
            actor=_actor(tenant_a),
            endpoint_id=endpoint.id,
        )
        assert new != old
        assert webhook_service._endpoint_secret(endpoint) == new


async def test_the_audit_entry_for_an_endpoint_never_carries_the_secret(
    app_session_factory, tenant_a
):
    """An auditor can read the chain, and an auditor's remit does not include
    the ability to forge our alerts."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        _, secret = await _endpoint(session, tenant_a)
        await session.flush()
        rows = await session.execute(
            select(AuditEvent).where(
                AuditEvent.action == AuditAction.WEBHOOK_ENDPOINT_CREATED
            )
        )
        # Read inside the session: these are ORM instances, and payload is
        # loaded lazily.
        payloads = [e.payload for e in rows.scalars().all()]

    assert payloads
    assert secret not in json.dumps(payloads)


def test_the_acknowledgement_scope_can_never_reach_a_browser():
    """A new scope is a new chance to widen the publishable ceiling by accident.

    `webhook:ack` is harmless in a server and wrong in a page: a published
    credential that can mark withdrawal alerts as acted-upon would let anybody
    who read the bundle silence the DPO's escalations for alerts nobody acted
    on — turning the one signal that catches a processor still processing into
    a clean dashboard.
    """
    from app.core.permissions import PUBLISHABLE_SCOPES, Scope

    assert Scope.WEBHOOK_ACK not in PUBLISHABLE_SCOPES
    assert PUBLISHABLE_SCOPES == frozenset({Scope.CONSENT_COLLECT})


# --------------------------------------------------------------------------- #
# Emitting
# --------------------------------------------------------------------------- #

async def test_emit_queues_one_delivery_per_subscribed_endpoint(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        await _endpoint(session, tenant_a, url="https://a.example.com/hook")
        await _endpoint(session, tenant_a, url="https://b.example.com/hook")

        queued = await webhook_service.emit(
            session,
            tenant_id=tenant_a["id"],
            event="consent.withdrawn",
            data={"principal_ref": "user:1", "purpose": "marketing"},
        )
        assert queued == 2


async def test_emit_skips_endpoints_that_did_not_subscribe(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        await _endpoint(session, tenant_a, events=["dsar.received"])
        queued = await webhook_service.emit(
            session, tenant_id=tenant_a["id"], event="consent.withdrawn", data={}
        )
        assert queued == 0


async def test_emit_skips_a_disabled_endpoint(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        endpoint, _ = await _endpoint(session, tenant_a)
        await webhook_service.set_active(
            session,
            tenant_id=tenant_a["id"],
            actor=_actor(tenant_a),
            endpoint_id=endpoint.id,
            active=False,
        )
        queued = await webhook_service.emit(
            session, tenant_id=tenant_a["id"], event="consent.withdrawn", data={}
        )
        assert queued == 0


async def test_emit_with_no_subscribers_is_a_normal_outcome(
    app_session_factory, tenant_a
):
    """Zero, not an error. Most workspaces have no subscribers and their
    withdrawals must not behave differently for it."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        assert await webhook_service.emit(
            session, tenant_id=tenant_a["id"], event="consent.withdrawn", data={}
        ) == 0


async def test_emit_refuses_an_event_name_that_does_not_exist(
    app_session_factory, tenant_a
):
    """Loud, because a typo at a call site means an alert that silently never
    fires — and the symptom of that is a processor nobody told."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        with pytest.raises(ValueError):
            await webhook_service.emit(
                session, tenant_id=tenant_a["id"], event="consent.gone", data={}
            )


# --------------------------------------------------------------------------- #
# The thing this feature exists for
# --------------------------------------------------------------------------- #

async def _principal_and_purpose(session, tenant):
    """A published purpose and somebody to hold consent for it.

    Mirrors `test_consent`'s setup rather than inserting rows directly: a
    consent needs a PUBLISHED notice to point at, and building one by hand is
    how a test ends up exercising a state the product cannot reach.
    """
    from app.services import notice_service

    purpose = await notice_service.create_purpose(
        session,
        tenant_id=tenant["id"],
        actor=_actor(tenant),
        key="marketing_email",
        name="Marketing email",
        category="Contact Data",
        legal_basis="consent",
    )
    notice = await notice_service.draft_notice(
        session,
        tenant_id=tenant["id"],
        actor=_actor(tenant),
        purpose_id=purpose.id,
        language="English",
        content="We use your email to send offers.",
        data_collected="Email address",
        user_rights="You may withdraw at any time.",
        withdrawal_policy="Marketing stops within 24 hours.",
    )
    await notice_service.publish_notice(
        session, tenant_id=tenant["id"], actor=_actor(tenant), notice_id=notice.id
    )
    principal = DataPrincipal(
        tenant_id=tenant["id"],
        external_id="user:42",
        email="person@example.com",
    )
    session.add(principal)
    await session.flush()
    return principal, purpose


async def test_withdrawing_consent_queues_an_alert_to_the_processor(
    app_session_factory, tenant_a
):
    """The whole point. Before this, a withdrawal changed a row and reached
    nothing — §6(6) says processing ceases, and a ledger that tells no system
    cannot bring that about."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        await _endpoint(session, tenant_a)
        principal, purpose = await _principal_and_purpose(session, tenant_a)

        await consent_service.grant(
            session,
            tenant_id=tenant_a["id"],
            actor=_actor(tenant_a),
            principal_id=principal.id,
            purpose_id=purpose.id,
        )
        await consent_service.withdraw(
            session,
            tenant_id=tenant_a["id"],
            actor=_actor(tenant_a),
            principal_id=principal.id,
            purpose_id=purpose.id,
        )

        rows = await session.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.event == "consent.withdrawn"
            )
        )
        payloads = [d.payload for d in rows.scalars().all()]

        assert len(payloads) == 1
        data = payloads[0]["data"]
        # Their identifier, not ours. A receiver cannot act on a DataShield uuid.
        assert data["principal_ref"] == "user:42"
        assert data["purpose"] == "marketing_email"


async def test_a_withdrawal_still_succeeds_when_a_subscriber_is_unreachable(
    app_session_factory, tenant_a
):
    """Emitting inserts rows and makes no network call, so there is no path by
    which a customer's broken endpoint fails a person's withdrawal.

    Asserted with an endpoint whose host does not resolve: if `emit` touched the
    network at all, this would raise or hang.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        await _endpoint(
            session, tenant_a, url="https://nx.invalid.example.test/hook"
        )
        principal, purpose = await _principal_and_purpose(session, tenant_a)
        await consent_service.grant(
            session,
            tenant_id=tenant_a["id"],
            actor=_actor(tenant_a),
            principal_id=principal.id,
            purpose_id=purpose.id,
        )

        consent = await consent_service.withdraw(
            session,
            tenant_id=tenant_a["id"],
            actor=_actor(tenant_a),
            principal_id=principal.id,
            purpose_id=purpose.id,
        )
        assert consent.status == "withdrawn"


# --------------------------------------------------------------------------- #
# Delivering
# --------------------------------------------------------------------------- #

async def test_a_delivered_alert_carries_a_verifiable_signature(
    app_session_factory, tenant_a
):
    """Reconstructed by the receiver's own verification routine, from the bytes
    that actually arrived."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["sig"] = request.headers[webhook_service.SIGNATURE_HEADER]
        seen["event"] = request.headers[webhook_service.EVENT_HEADER]
        return httpx.Response(200)

    async with scoped(app_session_factory, tenant_a["id"]) as session:
        endpoint, secret = await _endpoint(session, tenant_a)
        await webhook_service.emit(
            session, tenant_id=tenant_a["id"], event="consent.withdrawn",
            data={"principal_ref": "user:7"},
        )
        delivery = (
            await session.execute(select(WebhookDelivery))
        ).scalars().one()

        async with _client(handler) as client:
            landed = await webhook_service.attempt(
                session, delivery=delivery, endpoint=endpoint, client=client
            )

    assert landed
    assert seen["event"] == "consent.withdrawn"
    assert webhook_service.verify(secret, body=seen["body"], header=seen["sig"])
    assert json.loads(seen["body"])["data"]["principal_ref"] == "user:7"


async def test_a_failed_send_is_retried_with_a_later_next_attempt(
    app_session_factory, tenant_a
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with scoped(app_session_factory, tenant_a["id"]) as session:
        endpoint, _ = await _endpoint(session, tenant_a)
        await webhook_service.emit(
            session, tenant_id=tenant_a["id"], event="consent.withdrawn", data={}
        )
        delivery = (await session.execute(select(WebhookDelivery))).scalars().one()
        before = delivery.next_attempt_at

        async with _client(handler) as client:
            landed = await webhook_service.attempt(
                session, delivery=delivery, endpoint=endpoint, client=client
            )

        assert not landed
        assert delivery.status == "queued"
        assert delivery.attempts == 1
        assert delivery.next_attempt_at > before
        assert delivery.last_status_code == 500


async def test_retries_stop_at_the_cap_rather_than_forever(
    app_session_factory, tenant_a
):
    """A permanent failure that retried forever would starve the live alerts
    behind it — the same reasoning the notification queue states."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410)

    async with scoped(app_session_factory, tenant_a["id"]) as session:
        endpoint, _ = await _endpoint(session, tenant_a)
        await webhook_service.emit(
            session, tenant_id=tenant_a["id"], event="consent.withdrawn", data={}
        )
        delivery = (await session.execute(select(WebhookDelivery))).scalars().one()

        async with _client(handler) as client:
            for _ in range(webhook_service.MAX_ATTEMPTS):
                delivery.next_attempt_at = datetime.now(UTC)
                await webhook_service.attempt(
                    session, delivery=delivery, endpoint=endpoint, client=client
                )

        assert delivery.status == "failed"
        assert delivery.attempts == webhook_service.MAX_ATTEMPTS


async def test_an_endpoint_that_fails_its_budget_is_disabled(
    app_session_factory, tenant_a
):
    """Twenty consecutive refusals is a decommissioned endpoint nobody told us
    about, not a blip. Continuing to queue for it hides that this customer is no
    longer receiving withdrawal alerts at all."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with scoped(app_session_factory, tenant_a["id"]) as session:
        endpoint, _ = await _endpoint(session, tenant_a)

        async with _client(handler) as client:
            for _ in range(FAILURE_BUDGET):
                await webhook_service.emit(
                    session, tenant_id=tenant_a["id"],
                    event="consent.withdrawn", data={},
                )
                delivery = (
                    await session.execute(
                        select(WebhookDelivery)
                        .where(WebhookDelivery.status == "queued")
                        .limit(1)
                    )
                ).scalars().first()
                if delivery is None:
                    break
                await webhook_service.attempt(
                    session, delivery=delivery, endpoint=endpoint, client=client
                )

        assert endpoint.active is False
        assert endpoint.disabled_at is not None
        assert "consecutive failures" in endpoint.disabled_reason


async def test_one_success_clears_the_failure_budget(app_session_factory, tenant_a):
    """Otherwise an endpoint that fails intermittently over months eventually
    disables itself on a run of failures that never actually happened."""
    outcomes = iter([503, 503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(outcomes))

    async with scoped(app_session_factory, tenant_a["id"]) as session:
        endpoint, _ = await _endpoint(session, tenant_a)
        async with _client(handler) as client:
            for _ in range(3):
                await webhook_service.emit(
                    session, tenant_id=tenant_a["id"],
                    event="consent.withdrawn", data={},
                )
                delivery = (
                    await session.execute(
                        select(WebhookDelivery)
                        .where(WebhookDelivery.status == "queued")
                        .order_by(WebhookDelivery.created_at)
                        .limit(1)
                    )
                ).scalars().first()
                await webhook_service.attempt(
                    session, delivery=delivery, endpoint=endpoint, client=client
                )

        assert endpoint.consecutive_failures == 0


async def test_a_redirect_is_not_followed(app_session_factory, tenant_a):
    """Following one would re-target a request the SSRF guard already cleared,
    at a host it never saw."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://169.254.169.254/"})

    async with scoped(app_session_factory, tenant_a["id"]) as session:
        endpoint, _ = await _endpoint(session, tenant_a)
        await webhook_service.emit(
            session, tenant_id=tenant_a["id"], event="consent.withdrawn", data={}
        )
        delivery = (await session.execute(select(WebhookDelivery))).scalars().one()

        async with _client(handler) as client:
            landed = await webhook_service.attempt(
                session, delivery=delivery, endpoint=endpoint, client=client
            )

        # A 3xx is not a 2xx: it did not land, and nothing was sent to the target.
        assert not landed
        assert delivery.last_status_code == 302


# --------------------------------------------------------------------------- #
# Acknowledgement and escalation — BRD §4.4.2
# --------------------------------------------------------------------------- #

async def _delivered(session, tenant, *, when=None):
    endpoint, _ = await _endpoint(session, tenant)
    await webhook_service.emit(
        session, tenant_id=tenant["id"], event="consent.withdrawn", data={}
    )
    delivery = (
        await session.execute(
            select(WebhookDelivery).order_by(WebhookDelivery.created_at.desc()).limit(1)
        )
    ).scalars().one()
    delivery.status = "delivered"
    delivery.delivered_at = when or datetime.now(UTC)
    await session.flush()
    return endpoint, delivery


async def test_an_undelivered_alert_cannot_be_acknowledged(
    app_session_factory, tenant_a
):
    """A row acknowledged but never delivered would be evidence of something
    that did not happen."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        await _endpoint(session, tenant_a)
        await webhook_service.emit(
            session, tenant_id=tenant_a["id"], event="consent.withdrawn", data={}
        )
        delivery = (await session.execute(select(WebhookDelivery))).scalars().one()

        with pytest.raises(Exception):
            await webhook_service.acknowledge(
                session, tenant_id=tenant_a["id"], delivery_id=delivery.id
            )


async def test_acknowledging_twice_keeps_the_first_timestamp(
    app_session_factory, tenant_a
):
    """Overwriting it would move the evidence of when processing actually
    stopped to whenever the receiver's retry happened to fire."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        _, delivery = await _delivered(session, tenant_a)

        first = await webhook_service.acknowledge(
            session, tenant_id=tenant_a["id"], delivery_id=delivery.id, note="stopped"
        )
        at = first.acknowledged_at

        again = await webhook_service.acknowledge(
            session, tenant_id=tenant_a["id"], delivery_id=delivery.id, note="stopped"
        )
        assert again.acknowledged_at == at
        assert again.status == "acknowledged"


async def test_an_unacknowledged_alert_escalates_once_past_its_window(
    app_session_factory, tenant_a
):
    """The failure with no other symptom: our sends succeed, the log looks
    healthy, and the processor carries on mailing somebody who withdrew."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        _, delivery = await _delivered(
            session, tenant_a, when=datetime.now(UTC) - timedelta(hours=48)
        )

        assert await webhook_service.sweep_escalations(
            session, tenant_id=tenant_a["id"]
        ) == 1
        # Idempotent — the grievance sweep's discipline, for the same reason.
        assert await webhook_service.sweep_escalations(
            session, tenant_id=tenant_a["id"]
        ) == 0

        await session.refresh(delivery)
        assert delivery.escalated_at is not None


async def test_an_alert_inside_its_window_does_not_escalate(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        await _delivered(session, tenant_a, when=datetime.now(UTC) - timedelta(hours=1))
        assert await webhook_service.sweep_escalations(
            session, tenant_id=tenant_a["id"]
        ) == 0


async def test_an_acknowledged_alert_never_escalates(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        _, delivery = await _delivered(
            session, tenant_a, when=datetime.now(UTC) - timedelta(hours=48)
        )
        await webhook_service.acknowledge(
            session, tenant_id=tenant_a["id"], delivery_id=delivery.id
        )
        assert await webhook_service.sweep_escalations(
            session, tenant_id=tenant_a["id"]
        ) == 0


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #

async def test_a_replay_is_a_new_row_and_leaves_the_failure_on_the_record(
    app_session_factory, tenant_a
):
    """The failed attempt is evidence that a processor was unreachable when it
    mattered. Rewinding its counters would erase the only record of the gap."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        await _endpoint(session, tenant_a)
        await webhook_service.emit(
            session, tenant_id=tenant_a["id"], event="consent.withdrawn",
            data={"principal_ref": "user:9"},
        )
        original = (await session.execute(select(WebhookDelivery))).scalars().one()
        original.status = "failed"
        original.attempts = webhook_service.MAX_ATTEMPTS
        await session.flush()

        replayed = await webhook_service.replay(
            session, tenant_id=tenant_a["id"], delivery_id=original.id
        )

        assert replayed.id != original.id
        assert replayed.status == "queued"
        assert replayed.attempts == 0
        # The ORIGINAL payload, so a withdrawal since superseded is not turned
        # into an instruction to resume.
        assert replayed.payload == original.payload
        assert original.status == "failed"


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #

async def test_one_tenants_endpoints_are_invisible_to_another(
    app_session_factory, tenant_a, tenant_b
):
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        await _endpoint(session, tenant_a, url="https://a-only.example.com/hook")
        await session.commit()

    async with scoped(app_session_factory, tenant_b["id"]) as session:
        rows = await webhook_service.list_endpoints(
            session, tenant_id=tenant_b["id"]
        )
        assert rows == []


async def test_one_tenants_delivery_log_is_invisible_to_another(
    app_session_factory, tenant_a, tenant_b
):
    """The log is evidence. One customer proving what they did must never
    involve reading what another customer did."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        await _endpoint(session, tenant_a)
        await webhook_service.emit(
            session, tenant_id=tenant_a["id"], event="consent.withdrawn", data={}
        )
        await session.commit()

    async with scoped(app_session_factory, tenant_b["id"]) as session:
        assert await webhook_service.list_deliveries(
            session, tenant_id=tenant_b["id"]
        ) == []


async def test_a_tenant_cannot_acknowledge_another_tenants_delivery(
    app_session_factory, tenant_a, tenant_b
):
    """RLS makes the row invisible, so this is a 404 rather than a silent
    cross-tenant write."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        _, delivery = await _delivered(session, tenant_a)
        delivery_id = delivery.id
        await session.commit()

    async with scoped(app_session_factory, tenant_b["id"]) as session:
        with pytest.raises(Exception):
            await webhook_service.acknowledge(
                session, tenant_id=tenant_b["id"], delivery_id=delivery_id
            )
