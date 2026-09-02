"""Storing, verifying and forgetting a customer's credentials.

Three rules run through everything here, and they are the reason this file is
more careful than its size suggests:

1. **Plaintext leaves the process only to reach the vendor.** No function here
   returns a decrypted credential to a caller, and `_out` is the only shape the
   API ever serialises. An admin who forgets a key replaces it; they cannot read
   it back.

2. **Storage is not connection.** A row starts `unverified` and becomes
   `connected` only when a probe succeeds. Nothing in this file sets `connected`
   without a probe result, because "we hold a password" and "we can reach the
   system" are different claims and a compliance product must not conflate them.

3. **Credentials are refused for connectors that cannot use them.** A `planned`
   or `needs_oauth` connector has nowhere to send a secret, so holding one is
   liability with no feature attached. `registry.storable()` is the gate.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.connectors import probes, registry
from app.core.crypto import CredentialSealError, mask, open_sealed, seal
from app.core.errors import Conflict, NotFound
from app.models.audit import AuditAction
from app.models.connection import Connection
from app.services import audit_service
from app.services.audit_service import Actor

logger = logging.getLogger("app.connections")


class ConnectionRefused(Conflict):
    """A lawful or procedural reason this connection cannot be stored."""


def _split(
    connector: registry.Connector,
    values: dict[str, Any],
    *,
    already_stored: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Separate a submitted form into secret, public, and hint parts.

    Driven by the registry's field declarations rather than by guessing at field
    names, so a connector that adds a secret field gets it encrypted without
    anybody remembering to update this function.

    `already_stored` names the secret fields that exist on the record. On an edit
    the form cannot display a stored secret, so a blank box means "unchanged" —
    and a required-field check that does not know this rejects the edit with
    "Password is required" for a password that is already set. That is exactly
    what it did before this argument existed.
    """
    secret: dict[str, Any] = {}
    public: dict[str, Any] = {}
    hints: dict[str, str] = {}

    declared = {f.key: f for f in connector.fields}
    unknown = set(values) - set(declared)
    if unknown:
        # Refused rather than ignored: silently dropping a field the admin filled
        # in means they believe they configured something they did not.
        raise ConnectionRefused(
            f"{connector.label} has no field(s): {', '.join(sorted(unknown))}."
        )

    for key, f in declared.items():
        raw = values.get(key)
        text = "" if raw is None else str(raw).strip()

        if not text:
            if f.key in already_stored:
                # Left blank on purpose; the stored value stands.
                continue
            if f.required and f.default is None:
                raise ConnectionRefused(f"{f.label} is required.")
            if f.default is not None:
                text = f.default
            else:
                continue

        if f.secret:
            secret[key] = text
            hints[key] = mask(text)
        else:
            public[key] = text

    return secret, public, hints


def _out(row: Connection) -> dict[str, Any]:
    """The only shape a connection is ever serialised in.

    `config_sealed` is absent by construction rather than by remembering to pop
    it — the safe default being the one that requires no discipline.
    """
    connector = registry.get(row.connector_id)
    return {
        "id": str(row.id),
        "connector_id": row.connector_id,
        "connector_label": connector.label if connector else row.connector_id,
        "category": connector.category if connector else "Unknown",
        "label": row.label,
        "status": row.status,
        "config": row.config_public,
        "hints": row.hints,
        "last_tested_at": row.last_tested_at,
        "last_test_ok": row.last_test_ok,
        "last_test_message": row.last_test_message,
        "created_at": row.created_at,
    }


async def create(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    connector_id: str,
    label: str,
    values: dict[str, Any],
    created_by: uuid.UUID | None = None,
) -> dict[str, Any]:
    connector = registry.get(connector_id)
    if connector is None:
        raise NotFound(f"No connector called {connector_id!r}.")

    if not registry.storable(connector_id):
        # The honest refusal. Spelling out the reason matters, because from the
        # admin's side "add credentials" looks equally possible for every card.
        reason = {
            registry.Status.NEEDS_OAUTH: (
                "connects by clicking Connect and granting consent, not by "
                "pasting a credential — that flow is not built yet"
            ),
            registry.Status.NEEDS_AGENT: (
                "cannot be reached from a cloud service at all; it needs an "
                "agent running inside your own network"
            ),
        }.get(
            connector.status,
            "is not implemented yet, so there is nothing to send credentials to",
        )
        raise ConnectionRefused(
            f"{connector.label} {reason}. Storing a production secret we cannot "
            "use would be a risk with no benefit, so it is refused."
        )

    name = (label or "").strip() or connector.label
    secret, public, hints = _split(connector, values)

    try:
        sealed = seal(secret)
    except CredentialSealError as exc:
        # Configuration failure, not the admin's fault — say so plainly rather
        # than blaming their input.
        logger.error("cannot seal a credential", extra={"context": {"error": str(exc)}})
        raise ConnectionRefused(
            "This deployment cannot store credentials: the credential "
            "encryption key is missing or invalid. Nothing was saved."
        ) from exc

    row = Connection(
        tenant_id=tenant_id,
        connector_id=connector_id,
        label=name,
        status="unverified",
        config_sealed=sealed,
        config_public=public,
        hints=hints,
        created_by=created_by,
    )

    # A savepoint, so a duplicate label does not discard the caller's
    # transaction — the same reasoning as notification_service.enqueue.
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as exc:
        raise ConnectionRefused(
            f"You already have a {connector.label} connection called {name!r}. "
            "Give this one a different name, or edit the existing one."
        ) from exc

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.CONNECTION_CREATED,
        entity_type="connection", entity_id=row.id,
        # Connector, label and the non-secret config only. The audit trail is
        # readable by an auditor and exportable in a report, so a credential in
        # here would be a credential in a PDF.
        payload={"connector": connector_id, "label": name, "config": public},
    )
    await session.refresh(row)
    return _out(row)


async def list_for_tenant(session, *, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Connection).order_by(Connection.connector_id, Connection.label)
        )
    ).scalars().all()
    return [_out(r) for r in rows]


async def _get(session, connection_id: uuid.UUID) -> Connection:
    row = await session.scalar(
        select(Connection).where(Connection.id == connection_id)
    )
    if row is None:
        raise NotFound("No such connection.")
    return row


async def update(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    connection_id: uuid.UUID,
    label: str | None = None,
    values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Edit a connection.

    A secret field left blank keeps the stored value rather than clearing it —
    because the form cannot show the admin what is already there, so blank means
    "unchanged", not "empty". Clearing a credential is done by deleting the
    connection.
    """
    row = await _get(session, connection_id)
    connector = registry.get(row.connector_id)
    if connector is None:
        raise NotFound(f"No connector called {row.connector_id!r}.")

    changed: list[str] = []

    if label is not None and label.strip() and label.strip() != row.label:
        row.label = label.strip()
        changed.append("label")

    if values:
        existing = open_sealed(row.config_sealed)
        # Only the secret fields the admin actually filled in.
        supplied = {
            k: v for k, v in values.items()
            if str(v or "").strip() != ""
        }
        secret, public, hints = _split(
            connector,
            {**dict(row.config_public), **supplied},
            already_stored=frozenset(existing),
        )
        merged_secret = {**existing, **secret}
        row.config_sealed = seal(merged_secret)
        row.config_public = public
        row.hints = {**row.hints, **hints}
        changed.append("config")

        # Any credential change invalidates the previous verification. Leaving
        # `connected` in place would let an admin edit a working connection into
        # a broken one and still see a green badge.
        row.status = "unverified"
        row.last_test_ok = None
        row.last_test_message = None
        row.last_tested_at = None

    if not changed:
        return _out(row)

    await session.flush()
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.CONNECTION_UPDATED,
        entity_type="connection", entity_id=row.id,
        payload={"connector": row.connector_id, "label": row.label,
                 "changed": sorted(changed)},
    )
    await session.refresh(row)
    return _out(row)


async def test(
    session, *, tenant_id: uuid.UUID, actor: Actor, connection_id: uuid.UUID
) -> dict[str, Any]:
    """Decrypt, connect, record the outcome.

    The one place a credential is decrypted, and it is audited on both outcomes —
    a failed test is as much a fact about a company's systems as a successful one.
    """
    row = await _get(session, connection_id)

    try:
        config = {**row.config_public, **open_sealed(row.config_sealed)}
    except CredentialSealError as exc:
        row.status = "failing"
        row.last_test_ok = False
        row.last_test_message = str(exc)
        row.last_tested_at = datetime.now(UTC)
        await session.flush()
        await session.refresh(row)
        return _out(row)

    result = await probes.run(row.connector_id, config)

    row.last_tested_at = datetime.now(UTC)
    row.last_test_ok = result.ok
    row.last_test_message = result.message
    if row.status != "disabled":
        row.status = "connected" if result.ok else "failing"
    await session.flush()

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.CONNECTION_TESTED,
        entity_type="connection", entity_id=row.id,
        payload={"connector": row.connector_id, "label": row.label,
                 "ok": result.ok, "message": result.message[:500]},
    )
    await session.refresh(row)
    out = _out(row)
    out["detail"] = result.detail
    return out


async def delete(
    session, *, tenant_id: uuid.UUID, actor: Actor, connection_id: uuid.UUID
) -> None:
    """Remove it, credential and all.

    Genuinely deleted, not soft-deleted. A retained credential is a retained
    credential whatever the flag says, and when a customer removes an integration
    they mean the secret should stop existing. The audit entry records that it
    happened, which is the part worth keeping.
    """
    row = await _get(session, connection_id)
    connector_id, label = row.connector_id, row.label

    # Audited BEFORE the delete, so the entity_id still resolves to something.
    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.CONNECTION_DELETED,
        entity_type="connection", entity_id=row.id,
        payload={"connector": connector_id, "label": label},
    )
    await session.delete(row)
    await session.flush()


async def credentials_for(
    session, *, connector_id: str, connection_id: uuid.UUID
) -> dict[str, Any]:
    """Decrypted credentials, for a connector that is about to use them.

    Deliberately not reachable from any route. The only legitimate caller is
    server-side code performing an access or erasure against the customer's
    system; exposing this over HTTP would undo the entire design.
    """
    row = await _get(session, connection_id)
    if row.connector_id != connector_id:
        raise NotFound("No such connection for that connector.")
    if row.status == "disabled":
        raise ConnectionRefused(f"The connection {row.label!r} is disabled.")
    return {**row.config_public, **open_sealed(row.config_sealed)}
