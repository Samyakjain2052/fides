"""Phase 4 — the properties that make the public API safe to integrate against.

The endpoint behaviour is exercised over real HTTP during verification; these
tests pin the logic underneath it, where the failure modes are subtle and would
otherwise only show up as a customer's duplicate consent record.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.errors import Conflict, RateLimited
from app.core.permissions import Scope
from app.core.security import generate_api_key, parse_api_key
from app.db.session import set_tenant_context
from app.models.public_api import ApiRequestLog, IdempotencyKey
from app.services import api_key_service, public_api_service
from app.services.audit_service import Actor


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


async def _make_key(factory, tenant, scopes=(Scope.CONSENT_READ,)):
    async with scoped(factory, tenant["id"]) as s:
        row, full = await api_key_service.create_key(
            s,
            tenant_id=tenant["id"],
            actor=_actor(tenant),
            name="test-key",
            scopes=[x.value for x in scopes],
        )
        info = {"full": full, "id": row.id}
        await s.commit()
    return info


# --------------------------------------------------------------------------- #
# The key format — the bug that made API auth impossible
# --------------------------------------------------------------------------- #

def test_api_key_carries_its_tenant():
    """Without this, authentication cannot work at all.

    `api_keys` is under row-level security, so the lookup — which necessarily
    happens before any tenant context exists — matched zero rows and every valid
    key got a 401. The tenant travels in the key so the context can be bound
    first, exactly as it does in a refresh token.
    """
    tenant_id = uuid.uuid4()
    full, prefix, _hash = generate_api_key("live", tenant_id=tenant_id)
    assert full.startswith("ds_live_")
    parsed, returned = parse_api_key(full)
    assert parsed == tenant_id
    assert returned == full


def test_a_key_without_a_tenant_parses_to_none():
    """A legacy or malformed key yields None rather than raising, so the caller
    fails on the lookup — the same outcome by a less surprising route."""
    parsed, _ = parse_api_key("ds_live_nothingusefulhere")
    assert parsed is None
    assert parse_api_key("garbage")[0] is None


async def test_the_tenant_in_a_key_is_not_trusted(
    app_session_factory, tenant_a, tenant_b
):
    """Reading the tenant out of the key is not the same as believing it.

    A key rewritten to claim another tenant finds no row there, because the
    lookup hash is over the whole key and RLS scopes the query.
    """
    key = await _make_key(app_session_factory, tenant_a)
    forged = key["full"].replace(tenant_a["id"].hex, tenant_b["id"].hex)
    claimed, _ = parse_api_key(forged)
    assert claimed == tenant_b["id"], "the forged key does claim tenant B"

    async with scoped(app_session_factory, tenant_b["id"]) as s:
        from app.core.errors import AuthenticationError

        with pytest.raises(AuthenticationError):
            await api_key_service.authenticate_key(s, full_key=forged)


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #

async def test_a_repeated_key_replays_the_first_response(app_session_factory, tenant_a):
    key = await _make_key(app_session_factory, tenant_a)
    body = {"principal_ref": "cust-1", "purpose": "marketing_email", "granted": True}

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        assert await public_api_service.replay_or_reserve(
            s, tenant_id=tenant_a["id"], api_key_id=key["id"],
            key="idk-1", endpoint="POST /consent", body=body,
        ) is None, "first call must proceed"

        await public_api_service.store_response(
            s, tenant_id=tenant_a["id"], api_key_id=key["id"], key="idk-1",
            endpoint="POST /consent", body=body,
            status_code=201, response_body={"status": "active", "id": "abc"},
        )
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        replay = await public_api_service.replay_or_reserve(
            s, tenant_id=tenant_a["id"], api_key_id=key["id"],
            key="idk-1", endpoint="POST /consent", body=body,
        )
        assert replay is not None, "a retry must not create a second consent"
        status_code, stored = replay
        assert status_code == 201
        assert stored["status"] == "active"


async def test_same_key_different_body_is_refused(app_session_factory, tenant_a):
    """A client bug, surfaced rather than hidden.

    Replaying the old response here would silently drop the second request, and
    the customer would be missing a consent record with nothing to explain it.
    """
    key = await _make_key(app_session_factory, tenant_a)
    first = {"principal_ref": "cust-1", "purpose": "marketing_email", "granted": True}
    second = {"principal_ref": "cust-1", "purpose": "analytics", "granted": True}

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await public_api_service.store_response(
            s, tenant_id=tenant_a["id"], api_key_id=key["id"], key="idk-2",
            endpoint="POST /consent", body=first, status_code=201, response_body={},
        )
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(Conflict, match="different request"):
            await public_api_service.replay_or_reserve(
                s, tenant_id=tenant_a["id"], api_key_id=key["id"],
                key="idk-2", endpoint="POST /consent", body=second,
            )


def test_key_order_does_not_change_the_fingerprint():
    """Two JSON objects with the same fields in a different order are the same
    request. Treating them as different would reject legitimate retries from any
    client that does not preserve key order."""
    a = public_api_service.request_fingerprint("POST /x", {"b": 2, "a": 1})
    b = public_api_service.request_fingerprint("POST /x", {"a": 1, "b": 2})
    assert a == b
    assert a != public_api_service.request_fingerprint("POST /y", {"a": 1, "b": 2})


async def test_an_expired_key_is_reusable(app_session_factory, tenant_a):
    key = await _make_key(app_session_factory, tenant_a)
    body = {"x": 1}
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await public_api_service.store_response(
            s, tenant_id=tenant_a["id"], api_key_id=key["id"], key="idk-3",
            endpoint="POST /consent", body=body, status_code=201, response_body={},
        )
        stored = await s.scalar(select(IdempotencyKey).where(IdempotencyKey.key == "idk-3"))
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        assert await public_api_service.replay_or_reserve(
            s, tenant_id=tenant_a["id"], api_key_id=key["id"],
            key="idk-3", endpoint="POST /consent", body=body,
        ) is None, "past its TTL the key should behave as fresh"


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #

async def test_rate_limit_counts_in_the_window_and_refuses(app_session_factory, tenant_a):
    """Counted in Postgres, not in one process's memory — so the limit survives a
    restart and holds across replicas."""
    key = await _make_key(app_session_factory, tenant_a)

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        for _ in range(3):
            await public_api_service.log_request(
                s, tenant_id=tenant_a["id"], api_key_id=key["id"],
                method="GET", path="/public/v1/consent/check",
                status_code=200, duration_ms=1,
            )
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        limit, remaining = await public_api_service.enforce_rate_limit(
            s, tenant_id=tenant_a["id"], api_key=await _key_row(s, key["id"]), limit=10
        )
        assert (limit, remaining) == (10, 6), "3 used, this one, 6 left"

        with pytest.raises(RateLimited):
            await public_api_service.enforce_rate_limit(
                s, tenant_id=tenant_a["id"],
                api_key=await _key_row(s, key["id"]), limit=3,
            )


async def test_requests_outside_the_window_do_not_count(app_session_factory, tenant_a):
    key = await _make_key(app_session_factory, tenant_a)
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        # Set created_at at INSERT time. The log holds no UPDATE grant — which is
        # the point of it being append-only — so backdating an existing row is
        # exactly the thing the table refuses, and the other test asserts that.
        s.add(
            ApiRequestLog(
                tenant_id=tenant_a["id"], api_key_id=key["id"], method="GET",
                path="/x", status_code=200, duration_ms=1,
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _limit, remaining = await public_api_service.enforce_rate_limit(
            s, tenant_id=tenant_a["id"], api_key=await _key_row(s, key["id"]), limit=5
        )
        assert remaining == 4, "an hour-old request is outside a one-minute window"


async def _key_row(session, key_id):
    from app.models.api_key import ApiKey

    return await session.scalar(select(ApiKey).where(ApiKey.id == key_id))


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #

async def test_one_tenants_request_log_is_invisible_to_another(
    app_session_factory, tenant_a, tenant_b
):
    key = await _make_key(app_session_factory, tenant_a)
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await public_api_service.log_request(
            s, tenant_id=tenant_a["id"], api_key_id=key["id"],
            method="GET", path="/x", status_code=200, duration_ms=1,
            principal_ref="cust-1", purpose_key="marketing_email",
        )
        await s.commit()

    async with scoped(app_session_factory, tenant_b["id"]) as s:
        assert (await s.execute(select(ApiRequestLog))).scalars().all() == []
        assert (await s.execute(select(IdempotencyKey))).scalars().all() == []


async def test_the_request_log_cannot_be_rewritten(app_session_factory, tenant_a):
    """Append-and-read only, like the audit trail. Evidence of what a customer's
    systems asked us is not evidence if the application can edit it."""
    key = await _make_key(app_session_factory, tenant_a)
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await public_api_service.log_request(
            s, tenant_id=tenant_a["id"], api_key_id=key["id"],
            method="GET", path="/x", status_code=200, duration_ms=1,
        )
        await s.commit()

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError, ProgrammingError

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises((ProgrammingError, DBAPIError)):
            await s.execute(text("UPDATE api_request_log SET status_code = 500"))

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises((ProgrammingError, DBAPIError)):
            await s.execute(text("DELETE FROM api_request_log"))


async def test_every_public_api_table_has_an_rls_policy(app_session_factory, tenant_a):
    from sqlalchemy import text

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        rows = await s.execute(
            text(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid)
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname IN ('idempotency_keys','api_request_log')
                """
            )
        )
        found = {r[0]: (r[1], r[2], r[3]) for r in rows.all()}
        assert len(found) == 2, f"missing: {found.keys()}"
        for table, (enabled, forced, policies) in found.items():
            assert enabled, f"{table} has RLS disabled"
            assert forced, f"{table} does not FORCE RLS"
            assert policies >= 1, f"{table} has no policy"


async def test_the_log_does_not_store_request_bodies(app_session_factory, tenant_a):
    """A log holding request bodies is a second copy of everyone's personal data,
    with none of the consent machinery around it. The columns deliberately do not
    exist, so no future handler can decide to fill them."""
    from sqlalchemy import inspect as sa_inspect

    columns = {c.key for c in sa_inspect(ApiRequestLog).columns}
    for forbidden in ("request_body", "response_body", "body", "payload"):
        assert forbidden not in columns, f"api_request_log grew a {forbidden} column"
