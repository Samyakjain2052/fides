"""Publishable keys — the properties the threat model depends on.

The premise is that the key is PUBLIC: it ships in a browser bundle and anyone
can read it. So none of these tests assert that it is secret. They assert that it
is **incapable of harm** and that every record it creates is **attributable**,
which is where the trust actually comes from.

Endpoint-level, through the real ASGI app against the real database, because the
things being tested — a scope guard, an origin dependency, a CHECK constraint,
an RLS-ordered lookup — do not exist at the service layer alone.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.permissions import PUBLISHABLE_SCOPES, Scope
from app.core.security import (
    generate_publishable_key,
    hash_ip,
    mint_consent_token,
    parse_publishable_key,
)
from app.db.session import set_tenant_context
from app.models.audit import AuditEvent
from app.models.publishable_key import ConsentProvenance, PublishableKey
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
    """The real app, over ASGI. No network, but every dependency runs."""
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def workspace(app_session_factory, tenant_a):
    """A tenant with published purposes and a publishable key pinned to ORIGIN."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await notice_service.seed_default_purposes(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a)
        )
        key, full = await publishable_key_service.create_key(
            s,
            tenant_id=tenant_a["id"],
            actor=_actor(tenant_a),
            name="site-banner",
            allowed_origins=[ORIGIN],
        )
        tenant = await s.scalar(select(Tenant).where(Tenant.id == tenant_a["id"]))
        info = {
            "tenant_id": tenant_a["id"],
            "key_id": key.id,
            "key": full,
            "secret": tenant.consent_token_secret,
        }
        await s.commit()
    return info


def _headers(workspace, origin: str = ORIGIN, **extra) -> dict:
    return {
        "X-Publishable-Key": workspace["key"],
        "Origin": origin,
        "Content-Type": "application/json",
        **extra,
    }


# --------------------------------------------------------------------------- #
# The capability ceiling — the whole point
# --------------------------------------------------------------------------- #

def test_a_publishable_key_can_only_ever_collect():
    """The ceiling is a constant, not a default a caller can widen."""
    assert PUBLISHABLE_SCOPES == frozenset({Scope.CONSENT_COLLECT})
    assert Scope.CONSENT_WITHDRAW not in PUBLISHABLE_SCOPES


async def test_the_database_refuses_a_withdraw_capability(app_session_factory, tenant_a):
    """Belt and braces: the service caps capabilities, and so does a CHECK
    constraint. A console bug or a data fix must not be able to put a destructive
    capability into a browser bundle."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        full, prefix, lookup = generate_publishable_key("live", tenant_id=tenant_a["id"])
        s.add(
            PublishableKey(
                tenant_id=tenant_a["id"], name="forced", prefix=prefix, key=full,
                lookup_hash=lookup, capabilities=["consent:withdraw"],
                allowed_origins=[ORIGIN],
            )
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            await s.flush()


async def test_create_key_ignores_any_requested_capability(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        key, _full = await publishable_key_service.create_key(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            name="k", allowed_origins=[ORIGIN],
        )
        assert key.capabilities == ["consent:collect"]
        await s.commit()


async def test_a_publishable_key_cannot_withdraw(client, workspace):
    """The live hole this work closes.

    Forging a consent is bad. Destroying a real one is worse: it deletes genuine
    evidence and triggers the customer's downstream processing stops for someone
    who never asked. There is deliberately no withdraw path a publishable key can
    reach — the banner router has no such endpoint, and the secret-key path
    rejects the credential entirely.
    """
    resp = await client.post(
        "/public/v1/banner/consent",
        headers=_headers(workspace),
        json={"principal_ref": "v1", "purpose": "marketing_email"},
    )
    assert resp.status_code == 201, resp.text

    # There is no granted=false on the banner schema at all.
    resp = await client.post(
        "/public/v1/banner/consent",
        headers=_headers(workspace),
        json={"principal_ref": "v1", "purpose": "marketing_email", "granted": False},
    )
    # The extra field is ignored; the consent stays active rather than being
    # withdrawn by a field name the endpoint does not honour.
    assert resp.status_code == 201
    assert resp.json()["status"] == "active"

    # And the secret-key withdraw path refuses a publishable key outright.
    resp = await client.post(
        "/public/v1/consent",
        headers={"X-API-Key": workspace["key"], "Content-Type": "application/json"},
        json={"principal_ref": "v1", "purpose": "marketing_email", "granted": False},
    )
    assert resp.status_code == 401


async def test_a_collect_only_secret_key_cannot_withdraw_either(
    client, app_session_factory, tenant_a, workspace
):
    """403 naming the missing scope, in the same shape Phase 4 already uses."""
    from app.services import api_key_service

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _row, full = await api_key_service.create_key(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            name="collector", scopes=["consent:read", "consent:collect"],
        )
        await s.commit()

    resp = await client.post(
        "/public/v1/consent",
        headers={"X-API-Key": full, "Content-Type": "application/json"},
        json={"principal_ref": "v1", "purpose": "marketing_email", "granted": True},
    )
    assert resp.status_code == 201

    resp = await client.post(
        "/public/v1/consent",
        headers={"X-API-Key": full, "Content-Type": "application/json"},
        json={"principal_ref": "v1", "purpose": "marketing_email", "granted": False},
    )
    assert resp.status_code == 403
    assert resp.json()["required"] == ["consent:withdraw"]


# --------------------------------------------------------------------------- #
# The lookup must survive RLS — regression for a bug hit twice already
# --------------------------------------------------------------------------- #

def test_the_key_carries_its_tenant():
    tenant_id = uuid.uuid4()
    full, prefix, _lookup = generate_publishable_key("live", tenant_id=tenant_id)
    assert full.startswith("pk_live_"), "the prefix must be visibly publishable"
    assert parse_publishable_key(full) == tenant_id


async def test_a_publishable_key_lookup_returns_a_row_under_rls(
    app_session_factory, workspace
):
    """The regression guard.

    `publishable_keys` is under FORCE row-level security. A lookup made before
    tenant context is bound matches nothing, and every valid key would 401 — the
    exact failure already hit with refresh tokens and then with secret API keys.
    Binding the tenant read out of the key comes FIRST.
    """
    claimed = parse_publishable_key(workspace["key"])
    assert claimed == workspace["tenant_id"]

    async with scoped(app_session_factory, claimed) as s:
        row = await publishable_key_service.resolve_key(s, full_key=workspace["key"])
        assert row.id == workspace["key_id"]

    # And the same lookup with NO tenant context finds nothing, which is what
    # makes the ordering necessary rather than merely tidy.
    async with app_session_factory() as s:
        await s.begin()
        found = await s.scalar(
            select(PublishableKey).where(PublishableKey.id == workspace["key_id"])
        )
        assert found is None, "RLS should hide the row without tenant context"
        await s.rollback()


async def test_a_revoked_key_stops_working(client, app_session_factory, workspace):
    async with scoped(app_session_factory, workspace["tenant_id"]) as s:
        await publishable_key_service.revoke_key(
            s, tenant_id=workspace["tenant_id"], key_id=workspace["key_id"],
            actor=Actor(type="system"),
        )
        await s.commit()

    resp = await client.post(
        "/public/v1/banner/consent",
        headers=_headers(workspace),
        json={"principal_ref": "v1", "purpose": "marketing_email"},
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Origin pinning — defence-in-depth
# --------------------------------------------------------------------------- #

async def test_an_origin_outside_the_allowlist_is_refused(client, workspace):
    resp = await client.post(
        "/public/v1/banner/consent",
        headers=_headers(workspace, origin="https://evil.example"),
        json={"principal_ref": "v1", "purpose": "marketing_email"},
    )
    assert resp.status_code == 403
    assert "not allowed" in resp.json()["detail"]


async def test_a_missing_origin_is_refused(client, workspace):
    headers = _headers(workspace)
    headers.pop("Origin")
    resp = await client.post(
        "/public/v1/banner/consent", headers=headers,
        json={"principal_ref": "v1", "purpose": "marketing_email"},
    )
    assert resp.status_code == 403


async def test_origin_matching_ignores_a_trailing_path_and_case(app_session_factory, tenant_a):
    """An origin is scheme+host+port. `https://Example.com/banner` and
    `https://example.com` are the same origin, and treating them as different
    would fail a valid request for a reason nobody would guess."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        key, _ = await publishable_key_service.create_key(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            name="k", allowed_origins=["https://Example.com/"],
        )
        assert key.allowed_origins == ["https://example.com"]
        assert publishable_key_service.assert_origin_allowed(
            key, "https://EXAMPLE.com"
        ) == "https://example.com"
        await s.commit()


async def test_a_key_needs_at_least_one_origin(app_session_factory, tenant_a):
    from app.core.errors import Conflict

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(Conflict, match="at least one allowed origin"):
            await publishable_key_service.create_key(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                name="k", allowed_origins=[],
            )


# --------------------------------------------------------------------------- #
# Provenance — where trust in a public record actually comes from
# --------------------------------------------------------------------------- #

async def test_provenance_is_server_set_and_reaches_the_audit_chain(
    client, app_session_factory, workspace
):
    resp = await client.post(
        "/public/v1/banner/consent",
        headers=_headers(workspace, **{"User-Agent": "Mozilla/5.0 (test)"}),
        json={"principal_ref": "visitor-7", "purpose": "marketing_email",
              "source": "cookie-banner"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["collection_method"] == "publishable_key"
    assert body["strongly_bound"] is False
    assert body["server_receipt_id"].startswith("rcpt_")
    assert body["notice_version"] == 1

    async with scoped(app_session_factory, workspace["tenant_id"]) as s:
        prov = await s.scalar(
            select(ConsentProvenance).where(
                ConsentProvenance.server_receipt_id == body["server_receipt_id"]
            )
        )
        assert prov is not None
        assert prov.origin == ORIGIN
        assert prov.user_agent == "Mozilla/5.0 (test)"
        assert prov.notice_version == 1
        assert prov.publishable_key_id == workspace["key_id"]
        assert prov.received_at.tzinfo is not None, "received_at must be aware"

        # The IP is hashed, never stored raw.
        assert prov.ip_hash is not None
        assert len(prov.ip_hash) == 64
        assert "." not in prov.ip_hash and ":" not in prov.ip_hash

        # And it is in the tamper-evident chain, so deleting the provenance row
        # alone would leave a record that no longer matches the trail.
        entry = await s.scalar(
            select(AuditEvent).where(
                AuditEvent.entity_type == "consent_provenance",
                AuditEvent.entity_id == prov.id,
            )
        )
        assert entry is not None, "provenance must appear in the audit chain"
        assert entry.actor_type == "publishable_key"
        assert entry.payload["server_receipt_id"] == body["server_receipt_id"]
        assert entry.payload["origin"] == ORIGIN
        assert entry.payload["collection_method"] == "publishable_key"
        assert entry.hash, "and it must be chained"


async def test_a_client_cannot_supply_its_own_provenance(client, workspace):
    """A caller that could set its own provenance would be supplying its own
    alibi. The fields are not in the request schema, so an attempt is ignored
    rather than honoured."""
    resp = await client.post(
        "/public/v1/banner/consent",
        headers=_headers(workspace),
        json={
            "principal_ref": "visitor-8", "purpose": "marketing_email",
            "origin": "https://trusted.example",
            "ip_hash": "0" * 64,
            "collection_method": "signed_token",
            "strongly_bound": True,
            "server_receipt_id": "rcpt_forged",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["collection_method"] == "publishable_key", "client cannot upgrade this"
    assert body["strongly_bound"] is False
    assert body["server_receipt_id"] != "rcpt_forged"


async def test_provenance_cannot_be_rewritten(app_session_factory, client, workspace):
    """Append-and-read, like the audit trail. Where a consent came from is
    evidence, and evidence the application can edit is not evidence."""
    resp = await client.post(
        "/public/v1/banner/consent", headers=_headers(workspace),
        json={"principal_ref": "v9", "purpose": "marketing_email"},
    )
    assert resp.status_code == 201

    async with scoped(app_session_factory, workspace["tenant_id"]) as s:
        with pytest.raises(DBAPIError):
            await s.execute(text("UPDATE consent_provenance SET origin = 'x'"))
    async with scoped(app_session_factory, workspace["tenant_id"]) as s:
        with pytest.raises(DBAPIError):
            await s.execute(text("DELETE FROM consent_provenance"))


async def test_strong_binding_requires_a_signed_token(
    client, app_session_factory, workspace
):
    """The database refuses a record that claims a verified binding without the
    token that would have produced one."""
    # A consent has to exist for the provenance row to hang off, or the INSERT
    # below matches nothing and passes for the wrong reason.
    resp = await client.post(
        "/public/v1/banner/consent", headers=_headers(workspace),
        json={"principal_ref": "bind-test", "purpose": "marketing_email"},
    )
    assert resp.status_code == 201

    async with scoped(app_session_factory, workspace["tenant_id"]) as s:
        with pytest.raises((IntegrityError, DBAPIError)):
            await s.execute(
                text(
                    """
                    INSERT INTO consent_provenance
                      (id, tenant_id, consent_id, server_receipt_id, received_at,
                       collection_method, strongly_bound)
                    SELECT gen_random_uuid(), :t, c.id, 'rcpt_liar', now(),
                           'publishable_key', true
                    FROM consents c LIMIT 1
                    """
                ),
                {"t": str(workspace["tenant_id"])},
            )


# --------------------------------------------------------------------------- #
# The signed-token step-up
# --------------------------------------------------------------------------- #

async def test_a_valid_token_produces_a_strongly_bound_record(client, workspace):
    """With a token the principal is verified: the integrator's own server, which
    actually authenticated the person, vouched for it. The asserted value in the
    body is overridden by the bound one."""
    token = mint_consent_token(secret=workspace["secret"], principal_ref="real-person")
    resp = await client.post(
        "/public/v1/banner/consent",
        headers=_headers(workspace),
        json={"principal_ref": "someone-else", "purpose": "marketing_email",
              "consent_token": token},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["principal_ref"] == "real-person", "the token wins over the assertion"
    assert body["collection_method"] == "signed_token"
    assert body["strongly_bound"] is True


async def test_an_expired_token_is_refused(client, workspace):
    token = mint_consent_token(
        secret=workspace["secret"], principal_ref="p", ttl_seconds=-10
    )
    resp = await client.post(
        "/public/v1/banner/consent", headers=_headers(workspace),
        json={"principal_ref": "p", "purpose": "marketing_email",
              "consent_token": token},
    )
    assert resp.status_code == 403
    assert "expired" in resp.json()["detail"]


async def test_a_forged_signature_is_refused(client, workspace):
    """Signed with the wrong secret — the shape is right and the signature is not."""
    payload = {
        "exp": int(datetime.now(UTC).timestamp()) + 300,
        "nonce": "aaaa",
        "principal_ref": "p",
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")
    sig = hmac.new(b"not-the-secret", body.encode(), hashlib.sha256).digest()
    token = f"{body}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"

    resp = await client.post(
        "/public/v1/banner/consent", headers=_headers(workspace),
        json={"principal_ref": "p", "purpose": "marketing_email",
              "consent_token": token},
    )
    assert resp.status_code == 403
    assert "signature" in resp.json()["detail"]


async def test_absent_token_falls_back_to_publishable_collect(client, workspace):
    resp = await client.post(
        "/public/v1/banner/consent", headers=_headers(workspace),
        json={"principal_ref": "asserted-only", "purpose": "marketing_email"},
    )
    assert resp.status_code == 201
    assert resp.json()["collection_method"] == "publishable_key"
    assert resp.json()["strongly_bound"] is False


async def test_a_key_can_require_the_step_up(client, app_session_factory, tenant_a):
    """For sensitive purposes, an asserted principal_ref is not good enough and
    the key says so rather than the integrator remembering to send a token."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await notice_service.seed_default_purposes(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a)
        )
        _key, full = await publishable_key_service.create_key(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            name="sensitive", allowed_origins=[ORIGIN], require_signed_token=True,
        )
        await s.commit()

    resp = await client.post(
        "/public/v1/banner/consent",
        headers={"X-Publishable-Key": full, "Origin": ORIGIN,
                 "Content-Type": "application/json"},
        json={"principal_ref": "p", "purpose": "marketing_email"},
    )
    assert resp.status_code == 403
    assert resp.json()["required"] == ["consent_token"]


# --------------------------------------------------------------------------- #
# Rate limiting — per key and per IP
# --------------------------------------------------------------------------- #

async def test_the_per_ip_limit_trips(client, app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await notice_service.seed_default_purposes(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a)
        )
        _key, full = await publishable_key_service.create_key(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), name="tight",
            allowed_origins=[ORIGIN],
            rate_limit_per_minute=100, rate_limit_per_ip_per_minute=3,
        )
        await s.commit()

    headers = {"X-Publishable-Key": full, "Origin": ORIGIN,
               "Content-Type": "application/json"}
    codes = []
    for i in range(5):
        r = await client.post(
            "/public/v1/banner/consent", headers=headers,
            json={"principal_ref": f"v{i}", "purpose": "marketing_email"},
        )
        codes.append(r.status_code)
    assert 429 in codes, f"per-IP limit never tripped: {codes}"
    assert codes[:3] == [201, 201, 201], codes


async def test_the_per_key_limit_trips(client, app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await notice_service.seed_default_purposes(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a)
        )
        _key, full = await publishable_key_service.create_key(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), name="keycap",
            allowed_origins=[ORIGIN],
            rate_limit_per_minute=2, rate_limit_per_ip_per_minute=1000,
        )
        await s.commit()

    headers = {"X-Publishable-Key": full, "Origin": ORIGIN,
               "Content-Type": "application/json"}
    codes = []
    for i in range(4):
        r = await client.post(
            "/public/v1/banner/consent", headers=headers,
            json={"principal_ref": f"k{i}", "purpose": "marketing_email"},
        )
        codes.append(r.status_code)
    assert codes[:2] == [201, 201], codes
    assert 429 in codes[2:], codes


async def test_the_limiter_compares_aware_datetimes(app_session_factory, workspace):
    """Regression: the window query once bound an aware datetime against a column
    the ORM had typed as naive, and 500'd. Both must be aware."""
    from app.models.public_api import ApiRequestLog

    async with scoped(app_session_factory, workspace["tenant_id"]) as s:
        key = await s.scalar(
            select(PublishableKey).where(PublishableKey.id == workspace["key_id"])
        )
        s.add(
            ApiRequestLog(
                tenant_id=workspace["tenant_id"], publishable_key_id=key.id,
                method="POST", path="/x", status_code=201, duration_ms=1,
                ip_hash=hash_ip("1.2.3.4"),
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await s.commit()

    async with scoped(app_session_factory, workspace["tenant_id"]) as s:
        key = await s.scalar(
            select(PublishableKey).where(PublishableKey.id == workspace["key_id"])
        )
        from app.services import public_api_service

        limit, remaining = await public_api_service.enforce_publishable_rate_limits(
            s, key=key, ip_hash=hash_ip("1.2.3.4")
        )
        assert remaining >= 0
        # An hour-old row is outside a one-minute window.
        assert remaining == min(key.rate_limit_per_minute,
                                key.rate_limit_per_ip_per_minute) - 1


# --------------------------------------------------------------------------- #
# Idempotency — same contract as the Phase 4 public API
# --------------------------------------------------------------------------- #

async def test_idempotent_replay_on_the_banner_path(client, workspace):
    body = {"principal_ref": "idem-1", "purpose": "marketing_email"}
    headers = _headers(workspace, **{"Idempotency-Key": "banner-key-1"})

    first = await client.post("/public/v1/banner/consent", headers=headers, json=body)
    assert first.status_code == 201
    second = await client.post("/public/v1/banner/consent", headers=headers, json=body)
    assert second.status_code == 201
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.json()["given_at"] == first.json()["given_at"]
    assert second.json()["server_receipt_id"] == first.json()["server_receipt_id"]


async def test_same_idempotency_key_different_body_is_a_conflict(client, workspace):
    headers = _headers(workspace, **{"Idempotency-Key": "banner-key-2"})
    await client.post(
        "/public/v1/banner/consent", headers=headers,
        json={"principal_ref": "idem-2", "purpose": "marketing_email"},
    )
    resp = await client.post(
        "/public/v1/banner/consent", headers=headers,
        json={"principal_ref": "idem-2", "purpose": "analytics"},
    )
    assert resp.status_code == 409
    assert "different request" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# Banner discovery, and what it must not offer
# --------------------------------------------------------------------------- #

async def test_the_banner_is_not_offered_mandatory_purposes(client, workspace):
    """A mandatory purpose does not rest on consent. Showing a toggle for it is
    the dark pattern the DPDP Act is written against, so it is not returned."""
    resp = await client.get("/public/v1/banner/purposes", headers=_headers(workspace))
    assert resp.status_code == 200
    keys = {p["key"] for p in resp.json()}
    assert "marketing_email" in keys
    assert "analytics" in keys
    assert "account_creation" not in keys
    assert "kyc_verification" not in keys
    # And the wording is included, so a banner can show what is being agreed to.
    assert all(p["content"] and p["notice_version"] for p in resp.json())


async def test_collecting_for_a_mandatory_purpose_is_refused(client, workspace):
    resp = await client.post(
        "/public/v1/banner/consent", headers=_headers(workspace),
        json={"principal_ref": "v1", "purpose": "kyc_verification"},
    )
    assert resp.status_code == 409
    assert "not collected by consent" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# The path answers as an API, not as a web page
# --------------------------------------------------------------------------- #

async def test_the_public_path_returns_json_not_spa_html(client):
    """`/public/*` once fell through to the SPA and answered a machine caller with
    a 200 carrying index.html — the worst possible failure for an API client,
    because every status check passes and every parse fails."""
    resp = await client.post(
        "/public/v1/banner/consent",
        headers={"Content-Type": "application/json"},
        json={"principal_ref": "x", "purpose": "marketing_email"},
    )
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "<html" not in resp.text.lower()
    assert resp.json()["status"] == 401


def test_the_nginx_config_proxies_the_public_path():
    """The app answering correctly is not enough — the edge has to route it.

    Asserted against the config file because the failure mode is a deploy-time
    one: nginx serving the SPA fallback for /public/ cannot be caught by any test
    that only talks to the ASGI app.
    """
    import pathlib

    # Two locations: the repo layout when run from a checkout, and the read-only
    # mount the cms-test container provides.
    candidates = [
        pathlib.Path(__file__).resolve().parents[2] / "frontend" / "nginx.conf.template",
        pathlib.Path("/frontend/nginx.conf.template"),
    ]
    conf = next((c for c in candidates if c.exists()), None)
    assert conf is not None, f"nginx template not found in {candidates}"
    text_ = conf.read_text()
    assert "location /public/" in text_, "nginx does not proxy the public API"

    # Assert the PROPERTY, not the literal. This used to pin the exact string
    # `proxy_pass ${BACKEND_URL}/public/;`, which broke when the proxy moved to a
    # variable upstream — a change that fixed a real 502 and did not touch this
    # behaviour at all. A guard test that fails on a correct refactor teaches
    # people to edit the test without reading it.
    block = text_[text_.index("location /public/"):]
    block = block[: block.index("\n    location ") if "\n    location " in block else len(block)]

    assert "proxy_pass" in block, "the /public/ block does not proxy anywhere"
    assert "index.html" not in block, (
        "/public/ must not fall through to the SPA — a machine caller would get a "
        "200 carrying HTML, the worst possible failure for it"
    )
    # The path must arrive unrewritten: a published contract cannot depend on this
    # proxy's shape. Either the literal form or $request_uri satisfies that; a
    # rewrite that strips the prefix does not.
    preserves_path = "/public/" in block.split("proxy_pass", 1)[1].split(";", 1)[0] \
        or "$request_uri" in block
    assert preserves_path, (
        "the /public/ path is rewritten before it reaches the backend"
    )


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #

async def test_one_tenant_cannot_see_anothers_keys_or_provenance(
    app_session_factory, workspace, tenant_b
):
    async with scoped(app_session_factory, tenant_b["id"]) as s:
        assert (await s.execute(select(PublishableKey))).scalars().all() == []
        assert (await s.execute(select(ConsentProvenance))).scalars().all() == []


async def test_every_new_table_has_an_rls_policy(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        rows = await s.execute(
            text(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid)
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname IN ('publishable_keys','consent_provenance')
                """
            )
        )
        found = {r[0]: (r[1], r[2], r[3]) for r in rows.all()}
        assert len(found) == 2, f"missing: {found.keys()}"
        for table, (enabled, forced, policies) in found.items():
            assert enabled, f"{table} has RLS disabled"
            assert forced, f"{table} does not FORCE RLS"
            assert policies >= 1, f"{table} has no policy"
