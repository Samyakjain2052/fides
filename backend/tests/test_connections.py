"""Connections to a customer's own systems.

This is the one feature in the product that holds a customer's *live production
secrets* — payment gateway keys, cloud credentials, database passwords. So the
tests are weighted towards the things that would be a breach rather than a bug:
that plaintext never leaves, that a tenant cannot read another tenant's
credentials, and that credentials are refused for connectors that have nowhere to
send them.

The probe tests connect to the demo datastores this repo already runs
(`app-postgres`, `app-mysql`, `app-mongo`). That is deliberate: it is the
difference between a connector that is written and a connector that is known to
work, and it is why exactly three are marked `live`.
"""

from __future__ import annotations

import base64
import os
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text

from app.connectors import probes, registry
from app.db.session import set_tenant_context
from app.models.audit import AuditAction, AuditEvent
from app.models.connection import Connection
from app.services import connection_service
from app.services.audit_service import Actor

# A key for the suite. Real deployments read this from Key Vault.
TEST_KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    """Every test in this file needs a key, and needs the cache cleared so the
    setting is actually picked up rather than inherited from an earlier import."""
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


# --------------------------------------------------------------------------- #
# The cipher
# --------------------------------------------------------------------------- #

def test_a_credential_survives_a_round_trip():
    from app.core.crypto import open_sealed, seal

    payload = {"password": "s3cr3t-p@ss", "api_key": "rzp_live_abc123"}
    assert open_sealed(seal(payload)) == payload


def test_the_ciphertext_does_not_contain_the_plaintext():
    """The obvious property, asserted because getting it wrong is silent."""
    from app.core.crypto import seal

    sealed = seal({"password": "hunter2-correct-horse"})
    assert "hunter2" not in sealed
    assert "correct-horse" not in sealed


def test_two_seals_of_the_same_value_differ():
    """A fresh nonce each time. Identical ciphertexts would let anybody with
    read access see which customers share a password."""
    from app.core.crypto import seal

    a, b = seal({"password": "same"}), seal({"password": "same"})
    assert a != b


def test_a_tampered_ciphertext_is_refused_not_decrypted():
    """AES-GCM authenticates. Without that, a flipped bit yields garbage that
    some driver then tries to use as a password."""
    from app.core.crypto import CredentialSealError, open_sealed, seal

    sealed = seal({"password": "original"})
    scheme, encoded = sealed.split(":", 1)
    raw = bytearray(base64.b64decode(encoded))
    raw[-1] ^= 0x01
    tampered = f"{scheme}:{base64.b64encode(bytes(raw)).decode()}"

    with pytest.raises(CredentialSealError):
        open_sealed(tampered)


def test_a_different_key_cannot_open_it(monkeypatch):
    from app.core.config import get_settings
    from app.core.crypto import CredentialSealError, open_sealed, seal

    sealed = seal({"password": "original"})

    monkeypatch.setenv("DS_CREDENTIAL_ENCRYPTION_KEY",
                       base64.b64encode(b"z" * 32).decode())
    get_settings.cache_clear()
    with pytest.raises(CredentialSealError):
        open_sealed(sealed)


def test_a_short_key_is_refused_rather_than_padded(monkeypatch):
    """Padding a weak key produces something that looks like AES-256 and is not."""
    from app.core.config import get_settings
    from app.core.crypto import CredentialSealError, seal

    monkeypatch.setenv("DS_CREDENTIAL_ENCRYPTION_KEY", "too-short")
    get_settings.cache_clear()
    with pytest.raises(CredentialSealError):
        seal({"password": "x"})


def test_masking_does_not_reveal_a_short_secret():
    """The last four characters of a six-character secret is most of it."""
    from app.core.crypto import mask

    assert mask("abcdef") == "••••••"
    assert mask("rzp_live_abcd1234").endswith("1234")
    assert "rzp_live" not in mask("rzp_live_abcd1234")


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #

def test_every_connector_that_is_not_live_explains_itself():
    """A card saying "not available" must say why, or an admin will keep trying."""
    for c in registry.CONNECTORS:
        if c.status is registry.Status.LIVE:
            continue
        assert c.note or c.status is registry.Status.PLANNED, (
            f"{c.id} is {c.status} with no explanation"
        )


def test_only_live_and_beta_connectors_accept_credentials():
    """The gate that stops us holding a production secret we cannot use."""
    for c in registry.CONNECTORS:
        expected = c.status in (registry.Status.LIVE, registry.Status.BETA)
        assert registry.storable(c.id) is expected, c.id


def test_oauth_connectors_declare_no_credential_fields():
    """If one grew a form, somebody would fill it in and believe they had
    connected. OAuth needs a redirect, not a text box."""
    for c in registry.CONNECTORS:
        if c.auth is registry.AuthKind.OAUTH2:
            assert c.fields == (), f"{c.id} is OAuth but declares form fields"


def test_every_live_connector_has_a_probe():
    """`live` means verifiable. Without a probe it cannot be."""
    for c in registry.CONNECTORS:
        if c.status is registry.Status.LIVE:
            assert c.id in probes.PROBES, f"{c.id} is live with no probe"


def test_the_catalogue_carries_no_values_only_shapes():
    """It describes what a credential looks like, never one."""
    for item in registry.as_catalog():
        for f in item["fields"]:
            assert set(f) >= {"key", "label", "secret", "required"}
            assert "value" not in f


# --------------------------------------------------------------------------- #
# Storing
# --------------------------------------------------------------------------- #

async def test_a_stored_credential_is_not_readable_from_the_row(
    app_session_factory, tenant_a
):
    """The row holds ciphertext. Somebody with SELECT gets nothing usable."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await connection_service.create(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connector_id="postgresql", label="billing",
            values={"host": "db.example.com", "port": "5432",
                    "database": "billing", "user": "svc",
                    "password": "very-secret-password"},
        )
        row = await s.scalar(select(Connection))
        assert "very-secret-password" not in row.config_sealed
        # And it is not hiding in the clear columns either.
        assert "very-secret-password" not in str(row.config_public)
        assert "very-secret-password" not in str(row.hints)


async def test_the_api_shape_cannot_carry_a_credential(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        out = await connection_service.create(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connector_id="mysql", label="crm",
            values={"host": "mysql.example.com", "database": "crm",
                    "user": "svc", "password": "another-secret"},
        )
    assert "config_sealed" not in out
    assert "another-secret" not in str(out)
    # But the admin can still tell which key they pasted.
    assert out["hints"]["password"].endswith("cret")


async def test_a_new_connection_is_unverified_not_connected(
    app_session_factory, tenant_a
):
    """Storing a password is not connecting. The badge must not say otherwise."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        out = await connection_service.create(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connector_id="postgresql", label="x",
            values={"host": "h", "database": "d", "user": "u", "password": "p"},
        )
    assert out["status"] == "unverified"
    assert out["last_test_ok"] is None


@pytest.mark.parametrize("connector_id", ["razorpay", "zoho_crm", "tally"])
async def test_credentials_are_refused_for_connectors_that_cannot_use_them(
    app_session_factory, tenant_a, connector_id
):
    """planned / needs_oauth / needs_agent. Holding a live secret for any of
    these is risk with no feature attached."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(connection_service.ConnectionRefused) as caught:
            await connection_service.create(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                connector_id=connector_id, label="nope",
                values={},
            )
        # And the refusal explains itself.
        assert len(str(caught.value)) > 40


async def test_an_unknown_field_is_refused_not_ignored(
    app_session_factory, tenant_a
):
    """Silently dropping a field means the admin believes they configured
    something they did not."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(connection_service.ConnectionRefused):
            await connection_service.create(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                connector_id="postgresql", label="x",
                values={"host": "h", "database": "d", "user": "u",
                        "password": "p", "sslmode": "require"},
            )


async def test_a_missing_required_field_is_named(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        with pytest.raises(connection_service.ConnectionRefused, match="Password"):
            await connection_service.create(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                connector_id="postgresql", label="x",
                values={"host": "h", "database": "d", "user": "u"},
            )


async def test_a_duplicate_label_is_refused_without_losing_the_transaction(
    app_session_factory, tenant_a
):
    """The savepoint. A duplicate must not discard the caller's work."""
    vals = {"host": "h", "database": "d", "user": "u", "password": "p"}
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await connection_service.create(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connector_id="postgresql", label="same", values=vals,
        )
        with pytest.raises(connection_service.ConnectionRefused):
            await connection_service.create(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                connector_id="postgresql", label="same", values=vals,
            )
        # Session still usable, and a different label still works.
        out = await connection_service.create(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connector_id="postgresql", label="different", values=vals,
        )
        assert out["id"]


async def test_two_tenants_may_use_the_same_label(
    app_session_factory, tenant_a, tenant_b
):
    vals = {"host": "h", "database": "d", "user": "u", "password": "p"}
    for tenant in (tenant_a, tenant_b):
        async with scoped(app_session_factory, tenant["id"]) as s:
            out = await connection_service.create(
                s, tenant_id=tenant["id"], actor=_actor(tenant),
                connector_id="postgresql", label="production", values=vals,
            )
            assert out["label"] == "production"
            await s.commit()


async def test_a_tenant_cannot_see_another_tenants_connections(
    app_session_factory, tenant_a, tenant_b
):
    """The one that would be a breach rather than a bug."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await connection_service.create(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connector_id="postgresql", label="a-only",
            values={"host": "secret-host-a", "database": "d", "user": "u",
                    "password": "p"},
        )
        await s.commit()

    async with scoped(app_session_factory, tenant_b["id"]) as s:
        rows = await connection_service.list_for_tenant(s, tenant_id=tenant_b["id"])
        assert rows == []
        # Not even the raw table, because RLS is FORCEd.
        leaked = (await s.execute(text("SELECT count(*) FROM connections"))).scalar()
        assert leaked == 0


async def test_creating_a_connection_is_audited_without_the_secret(
    app_session_factory, tenant_a
):
    """The audit trail is readable by an auditor and exportable in a report, so a
    credential in a payload would be a credential in a PDF."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await connection_service.create(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connector_id="postgresql", label="audited",
            values={"host": "h", "database": "d", "user": "u",
                    "password": "do-not-log-me"},
        )
        events = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.CONNECTION_CREATED
                )
            )
        ).scalars().all()
        assert len(events) == 1
        assert "do-not-log-me" not in str(events[0].payload)
        assert events[0].payload["connector"] == "postgresql"


async def test_editing_a_credential_clears_the_previous_verification(
    app_session_factory, tenant_a
):
    """Otherwise an admin edits a working connection into a broken one and keeps
    a green badge."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        out = await connection_service.create(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connector_id="postgresql", label="edited",
            values={"host": "h", "database": "d", "user": "u", "password": "p"},
        )
        row = await s.scalar(select(Connection))
        row.status = "connected"
        row.last_test_ok = True
        await s.flush()

        after = await connection_service.update(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connection_id=uuid.UUID(out["id"]),
            values={"host": "new-host"},
        )
    assert after["status"] == "unverified"
    assert after["last_test_ok"] is None


async def test_a_blank_secret_on_edit_keeps_the_stored_one(
    app_session_factory, tenant_a
):
    """The form cannot display what is stored, so blank means unchanged."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        out = await connection_service.create(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connector_id="postgresql", label="keep",
            values={"host": "h", "database": "d", "user": "u",
                    "password": "keep-this-one"},
        )
        await connection_service.update(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connection_id=uuid.UUID(out["id"]),
            values={"host": "h2", "password": ""},
        )
        creds = await connection_service.credentials_for(
            s, connector_id="postgresql",
            connection_id=uuid.UUID(out["id"]),
        )
    assert creds["password"] == "keep-this-one"
    assert creds["host"] == "h2"


async def test_deleting_really_removes_the_credential(
    app_session_factory, tenant_a
):
    """Not soft-deleted. A retained credential is retained whatever the flag
    says, and removing an integration means the secret should stop existing."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        out = await connection_service.create(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connector_id="postgresql", label="gone",
            values={"host": "h", "database": "d", "user": "u", "password": "p"},
        )
        await connection_service.delete(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connection_id=uuid.UUID(out["id"]),
        )
        assert (await s.execute(text("SELECT count(*) FROM connections"))).scalar() == 0
        # The fact of the deletion survives, which is the part worth keeping.
        events = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.CONNECTION_DELETED
                )
            )
        ).scalars().all()
        assert len(events) == 1


# --------------------------------------------------------------------------- #
# Probes — against the demo datastores this repo actually runs.
#
# Skipped rather than failed when those containers are not up, so the suite still
# runs on a machine with only cms-db. A skip is honest; a pass would not be.
# --------------------------------------------------------------------------- #

def _reachable(host: str, port: int) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


async def test_the_postgres_probe_really_connects():
    if not _reachable("app-postgres", 5432):
        pytest.skip("app-postgres is not running")
    result = await probes.run("postgresql", {
        "host": "app-postgres", "port": "5432",
        "user": os.environ.get("APP_POSTGRES_USER") or "appuser",
        "password": os.environ.get("APP_POSTGRES_PASSWORD") or "apppassword",
        "database": os.environ.get("APP_POSTGRES_DB") or "appdb",
        "tls": "false",
    })
    assert result.ok, result.message
    assert "PostgreSQL" in (result.detail or {}).get("server", "")


async def test_a_wrong_password_fails_the_probe_without_leaking_it():
    """A failed probe must be reported, and must not echo the credential — some
    drivers put the whole connection string in the exception."""
    if not _reachable("app-postgres", 5432):
        pytest.skip("app-postgres is not running")
    result = await probes.run("postgresql", {
        "host": "app-postgres", "port": "5432", "user": "appuser",
        "password": "definitely-the-wrong-password", "database": "appdb",
        "tls": "false",
    })
    assert not result.ok
    assert "definitely-the-wrong-password" not in result.message


async def test_an_unroutable_host_times_out_with_an_explanation():
    """The commonest real failure: a customer's database on a private network.
    The message has to say that, not just "failed"."""
    result = await probes.run("postgresql", {
        # RFC 5737 documentation address: guaranteed not to route.
        "host": "192.0.2.1", "port": "5432", "user": "u",
        "password": "p", "database": "d", "tls": "false",
    })
    assert not result.ok
    assert "reachable" in result.message.lower() or "timed out" in result.message.lower()


async def test_a_connector_with_no_probe_says_so_rather_than_passing():
    """`planned` connectors must not report success by default."""
    result = await probes.run("razorpay", {"key_id": "x", "key_secret": "y"})
    assert not result.ok
    assert "no connection test" in result.message.lower()


async def test_a_successful_test_marks_the_connection_connected(
    app_session_factory, tenant_a
):
    """End to end: store, probe a real database, and record the outcome."""
    if not _reachable("app-postgres", 5432):
        pytest.skip("app-postgres is not running")

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        out = await connection_service.create(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connector_id="postgresql", label="demo-store",
            values={
                "host": "app-postgres", "port": "5432",
                "user": os.environ.get("APP_POSTGRES_USER") or "appuser",
                "password": os.environ.get("APP_POSTGRES_PASSWORD") or "apppassword",
                "database": os.environ.get("APP_POSTGRES_DB") or "appdb",
                "tls": "false",
            },
        )
        tested = await connection_service.test(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connection_id=uuid.UUID(out["id"]),
        )

    assert tested["status"] == "connected", tested["last_test_message"]
    assert tested["last_test_ok"] is True
    assert tested["last_tested_at"] is not None


async def test_a_failed_test_is_recorded_and_audited(
    app_session_factory, tenant_a
):
    """A connection that stopped working is a fact about a company's DSAR reach.
    Finding out during a statutory deadline is too late."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        out = await connection_service.create(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connector_id="postgresql", label="broken",
            values={"host": "192.0.2.1", "database": "d", "user": "u",
                    "password": "p", "tls": "false"},
        )
        tested = await connection_service.test(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connection_id=uuid.UUID(out["id"]),
        )
        assert tested["status"] == "failing"
        assert tested["last_test_ok"] is False

        events = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.CONNECTION_TESTED
                )
            )
        ).scalars().all()
        assert len(events) == 1
        assert events[0].payload["ok"] is False


# --------------------------------------------------------------------------- #
# Where a connector may connect.
#
# A connection test is a request to a host somebody else chose, which makes it an
# SSRF primitive unless it is fenced. This deployment made that concrete: the
# container apps share a VNet with the private endpoint to our OWN PostgreSQL
# server, so a tenant who pointed a "customer database" at that private address
# got back `password authentication failed for user "datashield_app"` — proof the
# server was there, proof the role name was real, and a password-guessing oracle
# against production from inside our network. Registration is open.
# --------------------------------------------------------------------------- #

@pytest.fixture
def _no_private_hosts(monkeypatch):
    """The deployed default. Local compose sets the opposite."""
    from app.core.config import get_settings

    monkeypatch.setenv("DS_CONNECTOR_ALLOW_PRIVATE_HOSTS", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("host", "because"),
    [
        ("10.30.8.4", "the private endpoint to our own database"),
        ("127.0.0.1", "loopback"),
        ("169.254.169.254", "the cloud metadata service that hands out tokens"),
        ("192.168.1.1", "a home router"),
        ("172.16.0.5", "private space"),
        ("0.0.0.0", "unspecified"),
    ],
)
async def test_a_probe_refuses_a_private_address(_no_private_hosts, host, because):
    result = await probes.run("postgresql", {
        "host": host, "port": "5432", "user": "u", "password": "p",
        "database": "d", "tls": "false",
    })
    assert not result.ok, f"{host} ({because}) was allowed"
    # And the refusal explains itself rather than looking like a network blip.
    assert "will not connect" in result.message


async def test_the_refusal_happens_before_any_connection_attempt(_no_private_hosts):
    """Refused by address, not by timing out. A guard that merely waits for a
    failed TCP connect still reveals whether the port was open."""
    import time

    started = time.monotonic()
    result = await probes.run("postgresql", {
        "host": "10.255.255.1", "port": "5432", "user": "u", "password": "p",
        "database": "d", "tls": "false",
    })
    elapsed = time.monotonic() - started
    assert not result.ok
    assert elapsed < 2.0, f"took {elapsed:.1f}s — it tried to connect"


@pytest.mark.parametrize("connector_id", ["postgresql", "mysql", "mongodb"])
async def test_every_live_connector_enforces_the_guard(_no_private_hosts, connector_id):
    """One probe forgetting the check is the whole hole reopened."""
    result = await probes.run(connector_id, {
        "host": "169.254.169.254", "port": "5432", "user": "u",
        "password": "p", "database": "d", "tls": "false",
    })
    assert not result.ok
    assert "will not connect" in result.message


def test_a_globally_routable_address_passes_the_guard(_no_private_hosts):
    """The guard must not refuse everything.

    Asserted on the guard itself rather than by probing, because it takes no
    network I/O to prove and a test that dials a real public host is a test that
    fails on a plane. Note the address choice: the RFC 5737 documentation ranges
    (192.0.2.0/24 and friends) are NOT usable here — Python's `ipaddress`
    classifies them as special-purpose, so the guard refuses them, correctly.
    """
    from app.connectors.hosts import resolve_and_check

    # A public resolver's well-known address. Checked, not connected to.
    addresses = resolve_and_check("8.8.8.8", 5432)
    assert "8.8.8.8" in addresses


def test_the_guard_checks_every_resolved_address(_no_private_hosts):
    """A name with both a public and a private A record must be refused. Checking
    only the first would let it through half the time."""
    from app.connectors.hosts import HostNotAllowed, resolve_and_check

    # localhost commonly resolves to both 127.0.0.1 and ::1; either must refuse.
    with pytest.raises(HostNotAllowed):
        resolve_and_check("localhost", 5432)


# --------------------------------------------------------------------------- #
# Health monitoring.
#
# Deliberately a scheduled job rather than something the admin page does on load.
# A probe is a real connection to a customer's production system with their
# credentials: on page load, a refresh authenticates against their database
# again, ten connections is ten simultaneous production connections, and once
# API connectors exist every page view spends their rate limit. The deciding
# objection is that a page-load check is only correct while somebody is looking —
# and the point of monitoring is knowing a connection broke at 3am, before a
# rights request arrives and its statutory clock starts.
# --------------------------------------------------------------------------- #

async def _make(session, tenant, host="192.0.2.1", label="mon"):
    return await connection_service.create(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        connector_id="postgresql", label=label,
        values={"host": host, "database": "d", "user": "u", "password": "p",
                "tls": "false"},
    )


async def test_a_failure_streak_counts_up(app_session_factory, tenant_a):
    """One failure is a blip; the count is what distinguishes it from an outage."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        out = await _make(s, tenant_a)
        row = await s.scalar(select(Connection))
        for expected in (1, 2, 3):
            await connection_service.health_check(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), row=row
            )
            assert row.consecutive_failures == expected
        assert row.status == "failing"


async def test_a_success_resets_the_streak_and_clears_the_alert(
    app_session_factory, tenant_a
):
    """Otherwise a connection that broke, was fixed, and broke again stays
    silent — the alert flag would still be set from the first outage."""
    if not _reachable("app-postgres", 5432):
        pytest.skip("app-postgres is not running")

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _make(s, tenant_a, host="192.0.2.1")
        row = await s.scalar(select(Connection))
        for _ in range(3):
            await connection_service.health_check(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), row=row
            )
        assert row.consecutive_failures == 3
        assert row.alerted_at is not None

        # Point it at something that works and check again.
        await connection_service.update(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connection_id=row.id,
            # The password has to be supplied too. Omitting it keeps the stored
            # one, which is correct behaviour and was the bug in this test.
            values={"host": "app-postgres", "port": "5432",
                    "database": os.environ.get("APP_POSTGRES_DB") or "appdb",
                    "user": os.environ.get("APP_POSTGRES_USER") or "appuser",
                    "password": os.environ.get("APP_POSTGRES_PASSWORD",
                                               "apppassword"),
                    "tls": "false"},
        )
        ok = await connection_service.health_check(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), row=row
        )
        assert ok, row.last_test_message
        assert row.consecutive_failures == 0
        assert row.alerted_at is None
        assert row.last_ok_at is not None
        assert row.status == "connected"


async def test_the_dpo_is_told_once_not_every_tick(app_session_factory, tenant_a):
    """Fifteen-minute reminders about a connection somebody is already fixing
    train people to filter the sender."""
    from app.models.notification import Notification

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        from app.services import notification_service as ns
        await ns.seed_default_templates(s, tenant_id=tenant_a["id"])

        await _make(s, tenant_a)
        row = await s.scalar(select(Connection))
        # Well past the threshold.
        for _ in range(6):
            await connection_service.health_check(
                s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), row=row
            )

        notices = (
            await s.execute(
                select(Notification).where(
                    Notification.template_key == "connection.failing"
                )
            )
        ).scalars().all()
        assert len(notices) == 1, f"sent {len(notices)} notices for one outage"


async def test_nothing_is_sent_before_the_threshold(app_session_factory, tenant_a):
    """A single blip must not page anybody."""
    from app.models.notification import Notification

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        from app.services import notification_service as ns
        await ns.seed_default_templates(s, tenant_id=tenant_a["id"])

        await _make(s, tenant_a)
        row = await s.scalar(select(Connection))
        await connection_service.health_check(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), row=row
        )
        notices = (
            await s.execute(
                select(Notification).where(
                    Notification.template_key == "connection.failing"
                )
            )
        ).scalars().all()
        assert notices == []


async def test_the_scheduled_job_and_the_button_record_the_same_way(
    app_session_factory, tenant_a
):
    """Both go through one recorder, so they cannot disagree about what a failure
    means or when somebody is told."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        out = await _make(s, tenant_a, label="via-button")
        via_button = await connection_service.test(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            connection_id=uuid.UUID(out["id"]),
        )
        assert via_button["consecutive_failures"] == 1
        assert via_button["status"] == "failing"


async def test_a_connection_with_monitoring_off_is_left_alone(
    app_session_factory, tenant_a
):
    """An admin who knows a system is down for maintenance can silence it
    without deleting the credential and re-entering it later."""
    from app.services.scheduler import check_connections

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _make(s, tenant_a)
        row = await s.scalar(select(Connection))
        row.monitor = False
        await s.commit()

    # The job selects on `monitor`, so this row is not in its work set.
    await check_connections()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        row = await s.scalar(select(Connection))
        assert row.last_tested_at is None
        assert row.consecutive_failures == 0
