"""Where one person's data sits across a workspace's connected systems.

Reached from a rights request and scoped to it. That scoping is the whole design
constraint: this is not a customer-data browser that happens to be filtered, it
is an answer to one request about one person, and there is no way to reach it
without a request that names them.

WHAT IT ANSWERS, AND WHAT IT REFUSES TO

  where      which systems and tables hold rows matching this person
  how much   row counts
  what kind  the categories the column names suggest — so an admin sees that a
             table holds Financial or Government-ID data before erasing it
  why        which identifier matched, and on which column, so a wrong match is
             visible rather than silently acted on

It does not return values. A rights request authorises acting on somebody's
data, not reading it, and an admin browsing a full customer record because a
request arrived is processing it for a new purpose. `PurgeRunItem` already
follows the same rule for this product's own tables: table, id, action, reason —
never a value.

THE RECEIPT LIVES ON THE REQUEST

No new receipts table. Every erasure writes a DSAR timeline event and an audit
entry, so the evidence sits with the request that caused it rather than in a
parallel log somebody has to correlate by timestamp.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.connectors import discovery, registry
from app.core.crypto import CredentialSealError, open_sealed
from app.core.errors import Conflict, NotFound
from app.models.audit import AuditAction
from app.models.connection import Connection
from app.models.consent import DataPrincipal
from app.models.dsar import DsarRequest
from app.services import audit_service
from app.services.audit_service import Actor

logger = logging.getLogger("app.data_map")


class ErasureRefused(Conflict):
    """A lawful or procedural reason this erasure cannot proceed."""


#: Categories where erasure is frequently unlawful rather than merely awkward.
#: Surfaced as a warning, never as a block: whether a statutory obligation
#: applies is the fiduciary's call, and a tool that silently refused would be
#: substituting its guess for their judgement.
STATUTORY_CATEGORIES = ("Financial", "Government ID", "Health")


async def _request_and_principal(
    session, *, request_id: uuid.UUID
) -> tuple[DsarRequest, DataPrincipal]:
    request = await session.scalar(
        select(DsarRequest).where(DsarRequest.id == request_id)
    )
    if request is None:
        raise NotFound("No such request.")
    principal = await session.scalar(
        select(DataPrincipal).where(DataPrincipal.id == request.principal_id)
    )
    if principal is None:
        raise NotFound("The person this request belongs to is no longer on record.")
    return request, principal


def _identifiers(principal: DataPrincipal) -> dict[str, str]:
    """What we can search a customer's systems by.

    Only what is actually on record — an absent phone number must not become an
    empty-string search that matches every row with a blank phone column.
    """
    out: dict[str, str] = {}
    if principal.email:
        out["email"] = principal.email
    if principal.phone:
        out["phone"] = principal.phone
    if principal.external_id and not principal.external_id.startswith(
        ("user:", "purged:")
    ):
        # `user:<uuid>` is this product's own key for a console user's principal
        # record, and `purged:` marks an already-erased one. Neither will appear
        # in a customer's database, and searching for them would waste a query
        # per table.
        out["external_id"] = principal.external_id
    return out


async def build(
    session, *, tenant_id: uuid.UUID, actor: Actor, request_id: uuid.UUID
) -> dict[str, Any]:
    """Discover where this person's data is, across every verified connection.

    Only `connected` connections are searched. An unverified or failing one is
    reported as unknown rather than skipped silently — "we did not look there"
    and "there is nothing there" are different answers, and conflating them is
    how an erasure gets reported as complete when it is not.
    """
    request, principal = await _request_and_principal(session, request_id=request_id)
    identifiers = _identifiers(principal)

    rows = (
        await session.execute(
            select(Connection).order_by(Connection.connector_id, Connection.label)
        )
    ).scalars().all()

    systems: list[dict[str, Any]] = []
    for row in rows:
        connector = registry.get(row.connector_id)
        entry: dict[str, Any] = {
            "connection_id": str(row.id),
            "connector_id": row.connector_id,
            "connector_label": connector.label if connector else row.connector_id,
            "label": row.label,
            "connection_status": row.status,
        }

        if row.status != "connected":
            entry |= {
                "ok": False,
                "error": (
                    f"This connection is {row.status}. Nothing was searched here, "
                    "so whether this person's data is in it is unknown — not "
                    "absent. Test the connection first."
                ),
                "findings": [], "total_rows": 0, "tables_scanned": 0,
                "truncated": False,
            }
            systems.append(entry)
            continue

        try:
            config = {**row.config_public, **open_sealed(row.config_sealed)}
        except CredentialSealError as exc:
            entry |= {
                "ok": False, "error": str(exc), "findings": [],
                "total_rows": 0, "tables_scanned": 0, "truncated": False,
            }
            systems.append(entry)
            continue

        result = await discovery.discover(row.connector_id, config, identifiers)
        entry |= result.as_dict()
        systems.append(entry)

    total_rows = sum(s.get("total_rows", 0) for s in systems)
    statutory = sorted({
        category
        for s in systems
        for f in s.get("findings", [])
        for category in f.get("categories", [])
        if category in STATUTORY_CATEGORIES
    })

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.DSAR_DATA_MAP_BUILT,
        entity_type="dsar_request", entity_id=request.id,
        # Counts and table names. No values, and no identifier beyond the ones
        # already on the request.
        payload={
            "reference": request.reference,
            "systems": len(systems),
            "rows_found": total_rows,
            "tables": [
                f"{s['label']}:{f['table']}"
                for s in systems for f in s.get("findings", [])
            ][:50],
        },
    )

    return {
        "request": {
            "id": str(request.id),
            "reference": request.reference,
            "type": request.type,
            "status": request.status,
            "deadline_at": request.deadline_at,
        },
        "person": {
            "id": str(principal.id),
            # The identifiers being searched by, shown so an admin can see the
            # match is against the right person.
            "email": principal.email,
            "phone": principal.phone,
            "external_id": principal.external_id,
            "legal_hold": principal.legal_hold,
            "legal_hold_reason": principal.legal_hold_reason,
            "already_purged_at": principal.purged_at,
        },
        "searched_by": sorted(identifiers),
        "systems": systems,
        "total_rows": total_rows,
        # Surfaced, not enforced. Whether a statutory obligation actually
        # applies is the fiduciary's decision to make and defend.
        "statutory_warning": statutory,
    }


async def erase(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    request_id: uuid.UUID,
    confirm_reference: str,
    only: list[str] | None = None,
) -> dict[str, Any]:
    """Mask this person out of the connected systems.

    `confirm_reference` must equal the request's own reference. The same guard
    the retention live run already uses: an irreversible action should not
    follow from a single unremarkable click, and typing the reference back means
    the admin was looking at the right request.

    `only` optionally names `"<connection_id>:<table>"` entries, so an admin who
    must retain one table for a statutory reason can erase the rest. Absent,
    everything found is erased.
    """
    request, principal = await _request_and_principal(session, request_id=request_id)

    if (confirm_reference or "").strip().upper() != request.reference.upper():
        raise ErasureRefused(
            f"To erase, type the request's reference ({request.reference}) to "
            "confirm. This cannot be undone, so it should not follow from a "
            "single click."
        )

    if request.type != "erasure":
        raise ErasureRefused(
            f"{request.reference} is a {request.type} request, not an erasure. "
            "Erasing on the strength of an access request would be acting "
            "beyond what the person asked for."
        )

    if principal.legal_hold:
        # The one hard block. A legal hold is somebody's considered decision
        # that this data must survive, usually because of litigation, and a
        # rights request does not outrank it — §12(3) exempts exactly this.
        raise ErasureRefused(
            "This person is under a legal hold "
            f"({principal.legal_hold_reason or 'no reason recorded'}), so their "
            "data cannot be erased. Lift the hold first, or reject the request "
            "with that as the recorded reason."
        )

    rows = (
        await session.execute(
            select(Connection).where(Connection.status == "connected")
        )
    ).scalars().all()

    identifiers = _identifiers(principal)
    outcomes: list[dict[str, Any]] = []
    total_affected = 0

    for row in rows:
        try:
            config = {**row.config_public, **open_sealed(row.config_sealed)}
        except CredentialSealError as exc:
            outcomes.append({
                "connection": row.label, "table": "—", "ok": False,
                "rows_affected": 0, "columns_masked": [], "error": str(exc),
            })
            continue

        found = await discovery.discover(row.connector_id, config, identifiers)
        if not found.ok:
            outcomes.append({
                "connection": row.label, "table": "—", "ok": False,
                "rows_affected": 0, "columns_masked": [],
                "error": found.error or "could not search this system",
            })
            continue

        for finding in found.findings:
            key = f"{row.id}:{finding.table}"
            if only is not None and key not in only:
                outcomes.append({
                    "connection": row.label, "table": finding.table, "ok": True,
                    "rows_affected": 0, "columns_masked": [],
                    "error": None, "skipped": "not selected",
                })
                continue

            value = identifiers.get(finding.matched_identifier, "")
            outcome = await discovery.erase(
                row.connector_id, config, finding, value, request.reference
            )
            total_affected += outcome.rows_affected
            outcomes.append({"connection": row.label, **outcome.as_dict()})

    # The receipt, on the request's own timeline. One event per table so the
    # record is per-table rather than a summary somebody has to trust.
    from app.services.dsar_service import _event

    for o in outcomes:
        if o.get("skipped"):
            note = f"{o['connection']} · {o['table']}: not selected, left alone"
        elif o["ok"]:
            note = (
                f"{o['connection']} · {o['table']}: masked "
                f"{o['rows_affected']} row(s), columns "
                f"{', '.join(o['columns_masked']) or '—'}"
            )
        else:
            note = f"{o['connection']} · {o['table']}: FAILED — {o['error']}"
        await _event(
            session, tenant_id=tenant_id, request=request, actor=actor,
            note=note, automated=False,
        )

    failures = [o for o in outcomes if not o["ok"]]

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.DSAR_CONNECTED_ERASURE,
        entity_type="dsar_request", entity_id=request.id,
        payload={
            "reference": request.reference,
            "rows_masked": total_affected,
            "tables": [
                {"connection": o["connection"], "table": o["table"],
                 "rows": o["rows_affected"], "ok": o["ok"]}
                for o in outcomes
            ][:50],
            "failures": len(failures),
        },
    )

    # Deliberately does NOT complete the request. Erasing the connected systems
    # is one part of fulfilling it; the person still has to be told, and whether
    # everything in scope was reached is a judgement the admin makes. Marking it
    # completed here would decide that for them.
    return {
        "reference": request.reference,
        "rows_masked": total_affected,
        "outcomes": outcomes,
        "failures": len(failures),
        "all_succeeded": not failures,
    }
