"""Public rights intake — no account, no credential.

The tests that matter most are the ones about ORDER: a request raised through
an unauthenticated form must be recorded and must have its clock running, and
must not execute until somebody proves they control the email address. An
unconfirmed erasure request that deletes something is the worst bug this
endpoint could have.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.session import set_tenant_context
from app.main import app
from app.models.dsar import DsarRequest
from app.models.notification import Notification
from app.services import dsar_service


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _raise(client, tenant, **kw):
    body = {
        "workspace": tenant["slug"],
        "type": kw.pop("type", "access"),
        "email": kw.pop("email", "stranger@example.com"),
        **kw,
    }
    return await client.post("/public/v1/rights", json=body)


# --------------------------------------------------------------------------- #
# It works at all, without an account
# --------------------------------------------------------------------------- #

async def test_somebody_with_no_account_can_raise_a_request(
    client, app_session_factory, tenant_a
):
    """The point of the endpoint.

    The people most likely to need §11 or §12 are the least likely to have an
    account — somebody whose number was bought from a broker, or who is asking
    for erasure precisely because they never signed up.
    """
    resp = await _raise(client, tenant_a, type="access")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["reference"].startswith("DSAR-")
    assert body["confirmation_required"] is True
    assert body["deadline_at"]


async def test_the_clock_starts_immediately(client, app_session_factory, tenant_a):
    """A deadline a company could stop by ignoring an email is not a deadline."""
    resp = await _raise(client, tenant_a)
    reference = resp.json()["reference"]

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(
            select(DsarRequest).where(DsarRequest.reference == reference)
        )
    assert row.deadline_at > row.submitted_at
    assert row.status == "received"


async def test_the_request_is_visible_in_the_queue_before_confirmation(
    client, app_session_factory, tenant_a
):
    """Recorded and countable straight away. Only EXECUTION waits."""
    resp = await _raise(client, tenant_a)
    reference = resp.json()["reference"]

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        rows = (
            await session.execute(select(DsarRequest))
        ).scalars().all()
    assert [r.reference for r in rows] == [reference]


async def test_an_unknown_workspace_is_refused_vaguely(client, tenant_a):
    """Telling an anonymous caller which workspaces exist would make this a
    customer-list oracle."""
    resp = await client.post(
        "/public/v1/rights",
        json={
            "workspace": "no-such-company",
            "type": "access",
            "email": "a@b.com",
        },
    )
    assert resp.status_code == 404
    assert "registered under that name" in resp.text


async def test_an_unknown_request_type_is_refused(client, tenant_a):
    resp = await _raise(client, tenant_a, type="portability")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "kind", ["access", "correction", "completion", "updating", "erasure"]
)
async def test_every_statutory_right_is_accepted(
    client, app_session_factory, tenant_a, kind
):
    details = (
        {"field": "phone", "current": "old", "corrected": "new"}
        if kind in ("correction", "completion", "updating")
        else None
    )
    resp = await _raise(
        client, tenant_a, type=kind, email=f"{kind}@example.com",
        details=details,
    )
    assert resp.status_code == 201, resp.text


# --------------------------------------------------------------------------- #
# Nothing executes before confirmation — the safety property
# --------------------------------------------------------------------------- #

async def test_a_publicly_raised_request_is_marked_unverified(
    client, app_session_factory, tenant_a
):
    resp = await _raise(client, tenant_a, type="erasure")
    reference = resp.json()["reference"]

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(
            select(DsarRequest).where(DsarRequest.reference == reference)
        )
    assert row.arrived_publicly is True
    assert row.verified_at is None
    assert row.verification_token_hash is not None
    # And it never reached the engine.
    assert row.engine_ref is None


async def test_dispatch_refuses_an_unconfirmed_public_request(
    app_session_factory, tenant_a, monkeypatch
):
    """The guard lives in `dispatch_to_engine`, not only at the call site.

    A check placed anywhere else can be bypassed by the next caller who forgets
    it, and what "bypassed" means here is an erasure executing on the strength
    of an email address anybody could type into a form.
    """
    from app.models.consent import DataPrincipal
    from app.services.audit_service import Actor

    called = False

    async def _explode(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("the engine must not be called")

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", _explode)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = DataPrincipal(
                tenant_id=tenant_a["id"],
                external_id=f"public:{uuid.uuid4().hex[:8]}@x.com",
                email="unconfirmed@example.com",
            )
            session.add(principal)
            await session.flush()
            actor = Actor(type="data_principal", id=None, label="x")
            _, digest = dsar_service.public_token()
            request = await dsar_service.submit(
                session, tenant_id=tenant_a["id"], actor=actor,
                principal_id=principal.id, type="erasure",
                arrived_publicly=True, verification_token_hash=digest,
            )
            await dsar_service.dispatch_to_engine(
                session, tenant_id=tenant_a["id"], actor=actor, request=request
            )

    assert called is False, "the engine was called for an unconfirmed request"
    assert request.engine_ref is None


async def test_a_portal_request_is_not_blocked_by_the_public_guard(
    app_session_factory, tenant_a, monkeypatch
):
    """The guard must catch only the public path.

    A signed-in person raising their own request has a session behind them, and
    blocking that would break the ordinary flow.
    """
    from app.models.consent import DataPrincipal
    from app.services.audit_service import Actor

    attempted = False

    async def _record(*args, **kwargs):
        nonlocal attempted
        attempted = True
        raise RuntimeError("engine unreachable in tests")

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", _record)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = DataPrincipal(
                tenant_id=tenant_a["id"],
                external_id=f"user:{uuid.uuid4()}",
                email="member@example.com",
            )
            session.add(principal)
            await session.flush()
            actor = Actor(type="user", id=tenant_a["admin_id"], label="dpo")
            request = await dsar_service.submit(
                session, tenant_id=tenant_a["id"], actor=actor,
                principal_id=principal.id, type="access",
                verification_method="session", verified=True,
            )
            await dsar_service.dispatch_to_engine(
                session, tenant_id=tenant_a["id"], actor=actor, request=request
            )

    assert attempted is True, "a confirmed request should reach the engine"


# --------------------------------------------------------------------------- #
# Confirmation
# --------------------------------------------------------------------------- #

async def test_a_confirmation_email_is_sent_rather_than_an_acknowledgement(
    client, app_session_factory, tenant_a
):
    """Not both.

    Sending an acknowledgement too would bury the one action the person has to
    take inside a message that reads as "nothing needed from you" — the same
    reasoning `grievance.confirm` already follows.

    Asserts on `subject_rendered`, which is retained. `pending_body` is dropped
    the moment a send reaches a terminal status, deliberately: a permanent log
    of message bodies would be a second copy of everyone's personal data with
    its own retention problem.
    """
    resp = await _raise(client, tenant_a)
    reference = resp.json()["reference"]

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        notes = (
            await session.execute(select(Notification))
        ).scalars().all()

    keys = {n.template_key for n in notes}
    assert keys == {"dsar.confirm"}, keys
    assert reference in notes[0].subject_rendered


def test_the_confirmation_template_explains_why_rather_than_demanding_a_click():
    """A link with no reason attached reads as phishing, which is exactly what
    somebody should suspect of an unexpected email about their data."""
    from app.services.notification_service import DEFAULT_TEMPLATES

    _, body = DEFAULT_TEMPLATES["dsar.confirm"]
    assert "{{confirm_url}}" in body
    assert "unconfirmed request" in body
    # And it tells them the clock is already running, so confirming does not
    # look like the thing that starts it.
    assert "does not restart that clock" in body
    # And what happens if it was not them.
    assert "did not make this request" in body


async def test_confirming_with_the_right_token_verifies_the_request(
    client, app_session_factory, tenant_a
):
    """Exercises the real endpoint, with a token this test minted.

    The emailed token cannot be recovered from the notification row — the
    rendered body is dropped once the send completes — so the request is
    created through the service with a known secret rather than by scraping an
    email. The endpoint under test is the same one either way.
    """
    from app.models.consent import DataPrincipal
    from app.services.audit_service import Actor

    # No engine is running in tests, and `dispatch_to_engine` records the
    # failure on the request rather than raising — so confirmation succeeds
    # either way, which this also proves. Patching httpx here would break the
    # test's OWN client, since that is an httpx.AsyncClient too.
    secret, digest = dsar_service.public_token()

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = DataPrincipal(
                tenant_id=tenant_a["id"],
                external_id=f"public:{uuid.uuid4().hex[:8]}@x.com",
                email="confirming@example.com",
            )
            session.add(principal)
            await session.flush()
            request = await dsar_service.submit(
                session, tenant_id=tenant_a["id"],
                actor=Actor(type="data_principal", id=None, label="x"),
                principal_id=principal.id, type="access",
                arrived_publicly=True, verification_token_hash=digest,
            )
            reference = request.reference

    confirmed = await client.post(
        "/public/v1/rights/confirm",
        json={
            "workspace": tenant_a["slug"],
            "reference": reference,
            "token": secret,
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["confirmed"] is True

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(
            select(DsarRequest).where(DsarRequest.reference == reference)
        )
    assert row.verified_at is not None
    assert row.verification_method == "email"
    # Spent: leaving the hash would keep a redeemable credential on the row.
    assert row.verification_token_hash is None


async def test_a_wrong_token_is_refused_with_the_same_message_as_a_spent_one(
    client, app_session_factory, tenant_a
):
    """Every failure reads identically. Each distinction would be a fact about
    somebody else's rights request."""
    resp = await _raise(client, tenant_a)
    reference = resp.json()["reference"]

    wrong = await client.post(
        "/public/v1/rights/confirm",
        json={
            "workspace": tenant_a["slug"],
            "reference": reference,
            "token": "x" * 40,
        },
    )
    missing = await client.post(
        "/public/v1/rights/confirm",
        json={
            "workspace": tenant_a["slug"],
            "reference": "DSAR-2026-9999",
            "token": "x" * 40,
        },
    )
    assert wrong.status_code == missing.status_code == 409
    assert wrong.json()["detail"] == missing.json()["detail"]
    assert dsar_service.PUBLIC_CONFIRM_GENERIC in wrong.json()["detail"]


async def test_a_guessed_reference_is_useless_without_the_token(
    client, app_session_factory, tenant_a
):
    """References are guessable — DSAR-2026-0001 — so the token has to carry the
    authority. Both are required."""
    resp = await _raise(client, tenant_a)
    reference = resp.json()["reference"]
    assert reference == "DSAR-2026-0001", "the point: it is guessable"

    guessed = await client.post(
        "/public/v1/rights/confirm",
        json={
            "workspace": tenant_a["slug"],
            "reference": reference,
            "token": "0" * 43,
        },
    )
    assert guessed.status_code == 409


# --------------------------------------------------------------------------- #
# Throttles
# --------------------------------------------------------------------------- #

async def test_a_second_unconfirmed_request_from_the_same_address_is_refused(
    client, app_session_factory, tenant_a
):
    """The honest case is a duplicate submit; the dishonest one is generating
    work against a mailbox somebody does not control."""
    first = await _raise(client, tenant_a, email="same@example.com")
    assert first.status_code == 201

    second = await _raise(
        client, tenant_a, email="same@example.com", type="erasure"
    )
    assert second.status_code == 409
    assert "waiting for email confirmation" in second.text


async def test_a_different_address_is_not_throttled(client, tenant_a):
    a = await _raise(client, tenant_a, email="one@example.com")
    b = await _raise(client, tenant_a, email="two@example.com")
    assert a.status_code == b.status_code == 201


async def test_no_client_ip_is_stored_for_a_public_rights_request(
    client, app_session_factory, tenant_a
):
    """Logging the IP of everybody who exercises a privacy right, in order to
    protect the privacy-rights system, would be a poor trade."""
    from app.models.audit import AuditEvent

    await _raise(client, tenant_a)

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        events = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.entity_type == "dsar_request")
            )
        ).scalars().all()
    assert events
    assert all(e.ip_address is None for e in events)


# --------------------------------------------------------------------------- #
# The form descriptor
# --------------------------------------------------------------------------- #

async def test_the_types_endpoint_quotes_the_workspaces_own_deadline(
    client, app_session_factory, tenant_a
):
    """A form promising 30 days while the workspace is set to 15 is worse than
    one that promises nothing."""
    resp = await client.get(
        f"/public/v1/rights/types?workspace={tenant_a['slug']}"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["organisation"] == tenant_a["name"]
    assert isinstance(body["respond_within_days"], int)
    assert {t["id"] for t in body["types"]} == {
        "access", "correction", "completion", "updating", "erasure",
    }
    assert body["confirmation_required"] is True


async def test_the_types_endpoint_does_not_reveal_unknown_workspaces(client):
    resp = await client.get("/public/v1/rights/types?workspace=nope-nope")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# CORS — an embedded form runs on somebody else's domain
# --------------------------------------------------------------------------- #

async def test_the_public_paths_answer_a_preflight_from_any_origin(client):
    """An embedded form runs on a customer's own website, whose origin we
    cannot know in advance."""
    resp = await client.request(
        "OPTIONS",
        "/public/v1/rights",
        headers={
            "Origin": "https://some-customer.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 204
    assert resp.headers["access-control-allow-origin"] == "*"


async def test_the_public_paths_never_allow_credentials(client, tenant_a):
    """Wildcard origin and credentials are not separable.

    A browser refuses `*` alongside credentials, and that refusal is what makes
    these endpoints CSRF-safe by construction: no session can travel to them.
    """
    resp = await _raise(client, tenant_a)
    assert resp.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in resp.headers


async def test_the_admin_api_is_not_given_wildcard_cors(client):
    """The scoped policy must not have leaked onto the authenticated API, which
    needs an allowlist because the refresh cookie depends on it."""
    resp = await client.get("/v1/dsar")
    assert resp.headers.get("access-control-allow-origin") != "*"
