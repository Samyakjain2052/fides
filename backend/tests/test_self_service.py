"""What a Data Principal can see about themselves, and nothing more.

These routes serve two different callers with one shape: a DPO looking at
somebody's consents, and that somebody looking at their own. They were gated on
`consent:read`, which is staff-only — so the Preference Centre and Consent
History, the two screens built for a Data Principal, answered a Data Principal
with 403.

It survived because those screens were still reading mock arrays. They rendered
invented numbers instead of the error, so the product looked like it worked for
the role it did not work for. Deleting the mock data is what surfaced it, and
these tests exist so it cannot come back quietly.

The other half matters just as much: opening a staff read to everybody, without
scoping the result, would turn a 403 into a way for any signed-in person to read
every other person's consent ledger by editing one query parameter. Both
directions are asserted below.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import select

from app.db.session import set_tenant_context
from app.models.consent import DataPrincipal
from app.services import notice_service
from app.services.audit_service import Actor

PASSWORD = "correct-horse-battery-staple-9"


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


@pytest.fixture
async def client():
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _sign_in(client, email: str, password: str, workspace: str) -> str:
    r = await client.post(
        "/v1/auth/login",
        json={"tenant_slug": workspace, "email": email, "password": password},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def person(app_session_factory, tenant_a, client):
    """A workspace with purposes, plus one Data Principal who can sign in."""
    from app.services import tenant_service

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await notice_service.seed_default_purposes(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a)
        )
        await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email="person@tenant-a.example.com",
            full_name="A Person", role="data_principal",
            password=PASSWORD, actor=_actor(tenant_a),
        )
        await s.commit()

    token = await _sign_in(
        client, "person@tenant-a.example.com", PASSWORD, tenant_a["slug"]
    )
    admin = await _sign_in(
        client, tenant_a["admin_email"], tenant_a["password"], tenant_a["slug"]
    )
    return {"token": token, "admin": admin}


# --------------------------------------------------------------------------- #
# Their own record
# --------------------------------------------------------------------------- #

async def test_a_person_can_fetch_their_own_principal_record(client, person):
    r = await client.get("/v1/principals/me", headers=_h(person["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["id"]


async def test_fetching_it_twice_returns_the_same_record(client, person):
    """Created on first read, not on every read.

    Two records for one human would give them two consent ledgers, and each
    screen would show half their history — which is exactly the bug the seeding
    script hit from the other side, by keying principals differently from the way
    the app resolves them.
    """
    first = await client.get("/v1/principals/me", headers=_h(person["token"]))
    second = await client.get("/v1/principals/me", headers=_h(person["token"]))
    assert first.json()["id"] == second.json()["id"]


async def test_the_key_comes_from_the_session_not_the_caller(
    client, person, app_session_factory, tenant_a
):
    """A GET with no body, deliberately.

    The screens used to POST `/principals` with an `external_id` they built
    themselves. Deriving it from the session instead means a caller cannot name
    somebody else's key and be handed their record.
    """
    r = await client.get("/v1/principals/me", headers=_h(person["token"]))
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        # Read inside the block. The context manager rolls back on exit, which
        # detaches the instance, and touching a column afterwards raises
        # DetachedInstanceError rather than returning the value.
        external_id = await s.scalar(
            select(DataPrincipal.external_id).where(
                DataPrincipal.id == r.json()["id"]
            )
        )
    assert external_id.startswith("user:")


# --------------------------------------------------------------------------- #
# What the Preference Centre needs
# --------------------------------------------------------------------------- #

async def test_a_person_can_read_the_purpose_and_notice_lists(client, person):
    """Neither is anybody's personal data, and a person cannot understand what
    they agreed to without both. The publishable-key banner already serves the
    same purposes to anonymous visitors."""
    for path in ("/v1/purposes", "/v1/notices"):
        r = await client.get(path, headers=_h(person["token"]))
        assert r.status_code == 200, f"{path}: {r.text}"


async def test_a_person_can_read_their_own_consents_and_history(client, person):
    me = await client.get("/v1/principals/me", headers=_h(person["token"]))
    pid = me.json()["id"]

    for path in (f"/v1/consents?principal_id={pid}",
                 f"/v1/consents/history?principal_id={pid}"):
        r = await client.get(path, headers=_h(person["token"]))
        assert r.status_code == 200, f"{path}: {r.text}"


async def test_the_whole_preference_centre_path_works_for_a_data_principal(
    client, person
):
    """The end-to-end assertion, because each call passing individually is not
    the thing that was broken — the screen was."""
    token = person["token"]
    me = await client.get("/v1/principals/me", headers=_h(token))
    assert me.status_code == 200
    pid = me.json()["id"]

    purposes = await client.get("/v1/purposes", headers=_h(token))
    notices = await client.get("/v1/notices?published_only=true", headers=_h(token))
    consents = await client.get(f"/v1/consents?principal_id={pid}", headers=_h(token))

    assert purposes.status_code == 200
    assert notices.status_code == 200
    assert consents.status_code == 200
    assert purposes.json(), "a seeded workspace has purposes to show"


# --------------------------------------------------------------------------- #
# And nothing more. The scoping half.
# --------------------------------------------------------------------------- #

async def test_a_person_cannot_read_somebody_elses_consents(
    client, person, app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        stranger = DataPrincipal(
            tenant_id=tenant_a["id"], external_id="someone-else",
            email="stranger@example.com",
        )
        s.add(stranger)
        await s.flush()
        stranger_id = str(stranger.id)
        await s.commit()

    r = await client.get(
        f"/v1/consents?principal_id={stranger_id}", headers=_h(person["token"])
    )
    assert r.status_code == 403, r.text


async def test_a_person_cannot_read_somebody_elses_history(
    client, person, app_session_factory, tenant_a
):
    """Asserted separately from the consents list. They are two routes, and one
    of them having the scoping call is not evidence the other does."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        stranger = DataPrincipal(
            tenant_id=tenant_a["id"], external_id="someone-else-2",
            email="stranger2@example.com",
        )
        s.add(stranger)
        await s.flush()
        stranger_id = str(stranger.id)
        await s.commit()

    r = await client.get(
        f"/v1/consents/history?principal_id={stranger_id}",
        headers=_h(person["token"]),
    )
    assert r.status_code == 403, r.text


async def test_a_person_still_cannot_list_every_principal(client, person):
    """`GET /principals` enumerates the whole workspace. Opening the self routes
    must not have opened this one."""
    r = await client.get("/v1/principals", headers=_h(person["token"]))
    assert r.status_code == 403, r.text


async def test_a_person_still_cannot_create_a_purpose(client, person):
    """The reads were loosened. The writes were not."""
    r = await client.post(
        "/v1/purposes",
        headers=_h(person["token"]),
        json={"key": "sneaky", "name": "Sneaky", "category": "Contact Data",
              "legal_basis": "consent"},
    )
    assert r.status_code == 403, r.text


# --------------------------------------------------------------------------- #
# Staff are unaffected
# --------------------------------------------------------------------------- #

async def test_staff_can_still_read_anybody(
    client, person, app_session_factory, tenant_a
):
    """The point of `require_any` is that one route serves both callers. If this
    regressed, every admin consent screen would break."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        somebody = DataPrincipal(
            tenant_id=tenant_a["id"], external_id="cust-999",
            email="cust999@example.com",
        )
        s.add(somebody)
        await s.flush()
        pid = str(somebody.id)
        await s.commit()

    for path in (f"/v1/consents?principal_id={pid}",
                 f"/v1/consents/history?principal_id={pid}",
                 "/v1/principals"):
        r = await client.get(path, headers=_h(person["admin"]))
        assert r.status_code == 200, f"{path}: {r.text}"
