"""Where one person's data is, and erasing it.

The tests are weighted towards the ways this feature could do harm rather than
the ways it could be inconvenient: erasing the wrong person, erasing under a
legal hold, erasing a statutory record, reporting an unsearched system as clean,
or leaking values through a screen that is supposed to show metadata.

Discovery and erasure run against `app-postgres`, which this repo ships — so
these assert on what actually happened in a real database, not on a mock.
"""

from __future__ import annotations

import base64
import os
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from app.connectors import discovery
from app.db.session import set_tenant_context
from app.models.audit import AuditAction, AuditEvent
from app.models.consent import DataPrincipal
from app.services import connection_service, data_map_service, dsar_service
from app.services.audit_service import Actor

TEST_KEY = base64.b64encode(b"m" * 32).decode()

# `or` rather than a get() default, deliberately.
#
# docker-compose passes these through as ${APP_POSTGRES_DB} etc. With no .env —
# which is exactly the case in CI — that expands to an EMPTY STRING, so the
# variable is set-but-empty and `os.environ.get(key, default)` returns "" and
# never the default. That produced `ConnectionRefused: Database is required.`
# in CI on a test that does not even need the database to be reachable.
PG = {
    "host": "app-postgres", "port": "5432",
    "user": os.environ.get("APP_POSTGRES_USER") or "appuser",
    "password": os.environ.get("APP_POSTGRES_PASSWORD") or "apppassword",
    "database": os.environ.get("APP_POSTGRES_DB") or "appdb",
    "tls": "false",
}


@pytest.fixture(autouse=True)
def _key(monkeypatch):
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


def _pg_up() -> bool:
    import socket

    try:
        with socket.create_connection(("app-postgres", 5432), timeout=2):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# The column-name heuristic
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("column", "expected"),
    [
        ("email", "email"),
        ("user_email", "email"),          # the demo store's orders table
        ("Email_Address", "email"),
        ("phone", "phone"),
        ("mobile_number", "phone"),
        ("customer_id", "external_id"),
        ("amount", None),                 # a value, not an identifier
        ("item", None),
        ("created_at", None),
    ],
)
def test_identifier_columns_are_recognised(column, expected):
    assert discovery.identifier_kind(column) == expected


def test_categories_flag_what_statute_protects():
    """An admin has to see "Financial" before erasing an orders table, because
    that is the case where erasure may be unlawful rather than awkward."""
    assert "Financial" in discovery.categories_for(["id", "amount", "item"])
    assert "Government ID" in discovery.categories_for(["name", "aadhaar"])
    assert "Identity" in discovery.categories_for(["full_name", "dob"])
    # And Government ID sorts before Contact, so it is read first.
    cats = discovery.categories_for(["email", "passport"])
    assert cats.index("Government ID") < cats.index("Contact")


def test_a_mask_leaves_the_business_record_alone():
    """The distinction the whole erasure design rests on: remove the person from
    the order, do not destroy the order."""
    masked = discovery.maskable_columns(
        ["id", "user_email", "amount", "item", "created_at"]
    )
    assert "user_email" in masked
    assert "amount" not in masked
    assert "item" not in masked
    assert "id" not in masked


# --------------------------------------------------------------------------- #
# Discovery, against a real database
# --------------------------------------------------------------------------- #

async def test_discovery_finds_one_person_under_two_column_names():
    """`users.email` and `orders.user_email` are the same person, and a map that
    found only one of them would under-report the erasure."""
    if not _pg_up():
        pytest.skip("app-postgres is not running")

    email = f"disc-{uuid.uuid4().hex[:8]}@example.com"
    await _seed(email, orders=2)

    result = await discovery.discover("postgresql", PG, {"email": email})
    assert result.ok, result.error
    tables = {f.table: f for f in result.findings}
    assert set(tables) == {"users", "orders"}
    assert tables["users"].rows == 1
    assert tables["orders"].rows == 2
    assert tables["orders"].matched_column == "user_email"
    assert tables["users"].matched_column == "email"


async def test_discovery_returns_no_values():
    """The property the whole screen depends on."""
    if not _pg_up():
        pytest.skip("app-postgres is not running")

    email = f"noval-{uuid.uuid4().hex[:8]}@example.com"
    await _seed(email, orders=1, name="Very Distinctive Name")

    result = await discovery.discover("postgresql", PG, {"email": email})
    blob = str([f.as_dict() for f in result.findings])
    assert "Very Distinctive Name" not in blob
    # The matched identifier is not echoed either — the caller already knows it.
    assert email not in blob


async def test_an_absent_identifier_is_not_searched_for():
    """An empty phone would match every row with a blank phone column."""
    result = await discovery.discover(
        "postgresql", PG, {"email": "", "phone": "", "external_id": ""}
    )
    assert not result.ok
    assert "nothing to search by" in (result.error or "")


async def test_a_connector_with_no_discovery_says_unknown_not_empty():
    result = await discovery.discover("razorpay", {}, {"email": "a@b.example"})
    assert not result.ok
    assert "unknown" in (result.error or "").lower()


# --------------------------------------------------------------------------- #
# Erasure, against a real database
# --------------------------------------------------------------------------- #

async def _seed(email: str, *, orders: int = 1, name: str = "Seeded Person") -> None:
    import asyncpg

    conn = await asyncpg.connect(
        host=PG["host"], port=5432, user=PG["user"], password=PG["password"],
        database=PG["database"], ssl=False, statement_cache_size=0,
    )
    try:
        await conn.execute(
            "INSERT INTO users (email, full_name, phone) VALUES ($1, $2, $3)",
            email, name, "+91-90000-00000",
        )
        for i in range(orders):
            await conn.execute(
                "INSERT INTO orders (user_email, amount, item) VALUES ($1, $2, $3)",
                email, 100 + i, f"Item {i}",
            )
    finally:
        await conn.close()


async def _count(email: str) -> tuple[int, int]:
    import asyncpg

    conn = await asyncpg.connect(
        host=PG["host"], port=5432, user=PG["user"], password=PG["password"],
        database=PG["database"], ssl=False, statement_cache_size=0,
    )
    try:
        users = await conn.fetchval(
            "SELECT count(*) FROM users WHERE email = $1", email
        )
        orders = await conn.fetchval(
            "SELECT count(*) FROM orders WHERE user_email = $1", email
        )
        return int(users), int(orders)
    finally:
        await conn.close()


async def test_erasure_removes_the_person_and_keeps_the_order():
    """The single most important assertion in this file.

    After erasure the person is unfindable, and the order's amount and item are
    still there — because those are the company's financial record, which they
    are often legally required to keep.
    """
    if not _pg_up():
        pytest.skip("app-postgres is not running")

    import asyncpg

    email = f"erase-{uuid.uuid4().hex[:8]}@example.com"
    await _seed(email, orders=2)
    assert await _count(email) == (1, 2)

    found = await discovery.discover("postgresql", PG, {"email": email})
    for finding in found.findings:
        outcome = await discovery.erase(
            "postgresql", PG, finding, email, "DSAR-2026-TEST"
        )
        assert outcome.ok, outcome.error

    assert await _count(email) == (0, 0), "the person is still findable"

    # And the business record survived.
    conn = await asyncpg.connect(
        host=PG["host"], port=5432, user=PG["user"], password=PG["password"],
        database=PG["database"], ssl=False, statement_cache_size=0,
    )
    try:
        rows = await conn.fetch(
            "SELECT amount, item FROM orders WHERE item IN ('Item 0', 'Item 1') "
            "AND user_email IS NULL"
        )
    finally:
        await conn.close()
    assert len(rows) >= 2, "the orders were destroyed instead of anonymised"


# --------------------------------------------------------------------------- #
# The request-scoped service: the guards
# --------------------------------------------------------------------------- #

async def _request_for(session, tenant, *, email, kind="erasure", hold=None):
    """A principal and a request for them, using the real service entry point."""
    principal = DataPrincipal(
        tenant_id=tenant["id"], external_id=f"dm-{uuid.uuid4().hex[:8]}",
        email=email,
    )
    if hold:
        principal.legal_hold = True
        principal.legal_hold_reason = hold
    session.add(principal)
    await session.flush()

    request = await dsar_service.submit(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        principal_id=principal.id, type=kind, requested_by_actor="staff",
    )
    return principal, request


async def test_erasure_is_refused_without_the_reference(
    app_session_factory, tenant_a
):
    """The same guard the retention live run uses. An action with no undo must
    not follow from a single unremarkable click."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _, request = await _request_for(s, tenant_a, email="refguard@example.com")
        with pytest.raises(data_map_service.ErasureRefused, match="reference"):
            await data_map_service.erase(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request_id=request.id, confirm_reference="NOT-THE-REFERENCE",
            )


async def test_erasure_is_refused_under_a_legal_hold(app_session_factory, tenant_a):
    """The one hard block. A legal hold is somebody's considered decision that
    this data must survive, and §12(3) exempts exactly that — a rights request
    does not outrank it."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _, request = await _request_for(
            s, tenant_a, email="held@example.com",
            hold="preserved for arbitration ARB-2026-11",
        )
        with pytest.raises(data_map_service.ErasureRefused) as caught:
            await data_map_service.erase(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request_id=request.id, confirm_reference=request.reference,
            )
        # The reason is quoted, so the admin can act on it rather than guess.
        assert "arbitration ARB-2026-11" in str(caught.value)


async def test_an_access_request_cannot_be_used_to_erase(
    app_session_factory, tenant_a
):
    """Erasing on the strength of an access request would be acting beyond what
    the person asked for."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _, request = await _request_for(
            s, tenant_a, email="accessonly@example.com", kind="access"
        )
        with pytest.raises(data_map_service.ErasureRefused, match="not an erasure"):
            await data_map_service.erase(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request_id=request.id, confirm_reference=request.reference,
            )


async def test_an_unverified_connection_is_reported_as_unknown(
    app_session_factory, tenant_a
):
    """"We did not look there" and "there is nothing there" are different
    answers, and conflating them is how an erasure gets reported as complete
    when it is not."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await connection_service.create(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connector_id="postgresql", label="never-tested", values=PG,
        )
        _, request = await _request_for(s, tenant_a, email="unknown@example.com")

        result = await data_map_service.build(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            request_id=request.id,
        )
        system = result["systems"][0]
        assert system["ok"] is False
        assert "unknown" in system["error"].lower()
        assert system["findings"] == []


async def test_building_a_map_is_audited_without_values(
    app_session_factory, tenant_a
):
    """Querying a customer's systems about one person IS processing, so it is
    recorded — with table names and counts, never a value."""
    if not _pg_up():
        pytest.skip("app-postgres is not running")

    email = f"audited-{uuid.uuid4().hex[:8]}@example.com"
    await _seed(email, orders=1, name="Audited Distinctive Person")

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        from app.models.connection import Connection

        await connection_service.create(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connector_id="postgresql", label="audited-store", values=PG,
        )
        row = await s.scalar(select(Connection))
        row.status = "connected"
        await s.flush()

        _, request = await _request_for(s, tenant_a, email=email)
        await data_map_service.build(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            request_id=request.id,
        )

        events = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.DSAR_DATA_MAP_BUILT
                )
            )
        ).scalars().all()
        payloads = [str(e.payload) for e in events]

    assert len(payloads) == 1
    assert "Audited Distinctive Person" not in payloads[0]
    assert "rows_found" in payloads[0]
