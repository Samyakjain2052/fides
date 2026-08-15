"""The banner collection contract, from the browser's side.

`test_publishable_keys.py` covers the credential and its threat model. This file
covers the *behaviour the two banner screens depend on* — the promises that, if
broken, turn a working consent banner into one that records nothing while
appearing to work.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import select

from app.db.session import set_tenant_context
from app.models.consent import Consent, DataPrincipal, Purpose
from app.models.publishable_key import ConsentProvenance
from app.models.tenant import Tenant
from app.services import notice_service, publishable_key_service
from app.services.audit_service import Actor

ORIGIN = "https://www.example.com"


def _actor(tenant: dict) -> Actor:
    return Actor(type="user", id=tenant["admin_id"], label="test")


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


@pytest.fixture
async def workspace(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await notice_service.seed_default_purposes(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a)
        )
        key, full = await publishable_key_service.create_key(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            name="site-banner", allowed_origins=[ORIGIN],
        )
        tenant = await s.scalar(select(Tenant).where(Tenant.id == tenant_a["id"]))
        info = {"tenant_id": tenant_a["id"], "key": full, "key_id": key.id,
                "secret": tenant.consent_token_secret}
        await s.commit()
    return info


def _h(ws, **extra):
    return {"X-Publishable-Key": ws["key"], "Origin": ORIGIN,
            "Content-Type": "application/json", **extra}


# --------------------------------------------------------------------------- #
# What the banner is allowed to render
# --------------------------------------------------------------------------- #

async def test_the_banner_gets_the_wording_it_must_display(client, workspace):
    """The banner shows the server's published notice text, not its own copy.

    The recorded consent is versioned against that exact text. A banner that
    renders hardcoded wording is showing one thing and recording agreement to
    another.
    """
    resp = await client.get("/public/v1/banner/purposes", headers=_h(workspace))
    assert resp.status_code == 200
    rows = resp.json()
    assert rows, "a seeded workspace must offer something"
    for r in rows:
        assert r["content"], "no wording to show"
        assert r["data_collected"] and r["user_rights"] and r["withdrawal_policy"]
        assert r["notice_version"] >= 1
        assert r["key"] and r["name"] and r["category"]


async def test_a_purpose_with_no_published_notice_is_not_offered(
    client, app_session_factory, tenant_a, workspace
):
    """Consent cannot lawfully be collected against text that was never
    published, so the banner must not offer it at all."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        purpose = await notice_service.create_purpose(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            key="unpublished_thing", name="Unpublished Thing", category="Usage Data",
        )
        # A draft only — deliberately never published.
        await notice_service.draft_notice(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            purpose_id=purpose.id, content="Draft wording that nobody has approved.",
            data_collected="x", user_rights="x", withdrawal_policy="x",
        )
        await s.commit()

    resp = await client.get("/public/v1/banner/purposes", headers=_h(workspace))
    assert "unpublished_thing" not in {r["key"] for r in resp.json()}


# --------------------------------------------------------------------------- #
# Nothing is consented by default, and declining writes nothing
# --------------------------------------------------------------------------- #

async def test_rendering_the_banner_records_nothing(
    client, app_session_factory, workspace
):
    """Loading a banner is not an act of consent.

    The screen initialises every toggle to false, but the guarantee that matters
    is the server one: fetching the options must not create a consent row.
    """
    await client.get("/public/v1/banner/purposes", headers=_h(workspace))
    async with scoped(app_session_factory, workspace["tenant_id"]) as s:
        assert (await s.execute(select(Consent))).scalars().all() == []


async def test_declining_is_the_absence_of_a_call(
    client, app_session_factory, workspace
):
    """A declined purpose produces no request at all — not a withdrawal.

    This is the whole reason the banner API has no `granted: false`. If a
    decline were a write, a published credential would have a destructive
    capability by the back door. Collecting one purpose must leave the other
    untouched rather than explicitly refused.
    """
    resp = await client.post(
        "/public/v1/banner/consent", headers=_h(workspace),
        json={"principal_ref": "visitor:abc", "purpose": "marketing_email",
              "source": "consent-banner"},
    )
    assert resp.status_code == 201

    async with scoped(app_session_factory, workspace["tenant_id"]) as s:
        rows = (await s.execute(select(Consent))).scalars().all()
        assert len(rows) == 1, "only the accepted purpose should exist"
        purpose = await s.scalar(select(Purpose).where(Purpose.id == rows[0].purpose_id))
        assert purpose.key == "marketing_email"
        assert rows[0].status == "active"
        # `analytics` was declined: there is no row for it in any state.
        assert not any(r.status == "withdrawn" for r in rows)


# --------------------------------------------------------------------------- #
# The visitor identity the banner asserts
# --------------------------------------------------------------------------- #

async def test_an_anonymous_visitor_ref_creates_a_principal(
    client, app_session_factory, workspace
):
    """A first-time visitor has no account. The browser-generated reference is
    what lets them see the same choices on a later visit, and the server creates
    the principal on first use rather than demanding a registration step."""
    resp = await client.post(
        "/public/v1/banner/consent", headers=_h(workspace),
        json={"principal_ref": "visitor:11111111-2222-3333-4444-555555555555",
              "purpose": "analytics"},
    )
    assert resp.status_code == 201

    async with scoped(app_session_factory, workspace["tenant_id"]) as s:
        p = await s.scalar(
            select(DataPrincipal).where(
                DataPrincipal.external_id
                == "visitor:11111111-2222-3333-4444-555555555555"
            )
        )
        assert p is not None


async def test_the_same_visitor_returning_updates_rather_than_duplicates(
    client, app_session_factory, workspace
):
    """A visitor who comes back and changes their mind should have one current
    answer per purpose, not a pile of rows — that is what makes /consent/check a
    single fast lookup."""
    for _ in range(2):
        r = await client.post(
            "/public/v1/banner/consent", headers=_h(workspace),
            json={"principal_ref": "visitor:same", "purpose": "analytics"},
        )
        assert r.status_code == 201

    async with scoped(app_session_factory, workspace["tenant_id"]) as s:
        rows = (await s.execute(select(Consent))).scalars().all()
        assert len(rows) == 1, "one current answer per (principal, purpose)"
        # But both acts are in the provenance history.
        prov = (await s.execute(select(ConsentProvenance))).scalars().all()
        assert len(prov) == 2, "each collection event keeps its own provenance"


# --------------------------------------------------------------------------- #
# Errors the banner has to be able to explain
# --------------------------------------------------------------------------- #

async def test_a_mandatory_purpose_is_refused_with_a_readable_reason(
    client, workspace
):
    """If the screen ever offers one by mistake, the refusal has to be something
    it can put in front of a person."""
    resp = await client.post(
        "/public/v1/banner/consent", headers=_h(workspace),
        json={"principal_ref": "visitor:x", "purpose": "kyc_verification"},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "not collected by consent" in detail
    assert "legal obligation" in detail


async def test_an_unknown_purpose_says_so(client, workspace):
    resp = await client.post(
        "/public/v1/banner/consent", headers=_h(workspace),
        json={"principal_ref": "visitor:x", "purpose": "no_such_purpose"},
    )
    assert resp.status_code == 404
    assert "no_such_purpose" in resp.json()["detail"]


async def test_every_error_is_json_with_a_detail(client, workspace):
    """The banner surfaces `detail` verbatim. If any of these came back as HTML
    or without a detail, the screen would have nothing to show but a status
    code."""
    cases = [
        (_h(workspace, Origin="https://evil.example"), 403),
        ({"Content-Type": "application/json"}, 401),
    ]
    for headers, expected in cases:
        resp = await client.post(
            "/public/v1/banner/consent", headers=headers,
            json={"principal_ref": "v", "purpose": "analytics"},
        )
        assert resp.status_code == expected
        assert resp.headers["content-type"].startswith("application/problem+json")
        assert resp.json().get("detail"), "no message the banner could display"


# --------------------------------------------------------------------------- #
# The receipt the banner shows back
# --------------------------------------------------------------------------- #

async def test_the_response_carries_everything_the_confirmation_needs(
    client, workspace
):
    """The screen shows a receipt, not a claim. Everything it displays has to be
    in the response."""
    resp = await client.post(
        "/public/v1/banner/consent", headers=_h(workspace),
        json={"principal_ref": "visitor:receipt", "purpose": "marketing_email",
              "language": "English", "source": "cookie-banner"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["server_receipt_id"].startswith("rcpt_")
    assert body["notice_version"] >= 1
    assert body["language"] == "English"
    assert body["status"] == "active"
    assert body["collection_method"] == "publishable_key"
    assert body["strongly_bound"] is False
    assert body["expires_at"], "retention drives the renewal date the banner shows"
