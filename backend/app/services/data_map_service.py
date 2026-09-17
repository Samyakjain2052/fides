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


class CorrectionRefused(Conflict):
    """A lawful or safety reason this correction cannot be carried out."""


async def correct(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    request_id: uuid.UUID,
    connection_id: uuid.UUID,
    table: str,
    column: str,
    new_value: str,
    confirm_reference: str | None = None,
    dry_run: bool = True,
    _automatic: bool = False,
) -> dict[str, Any]:
    """Carry out a §12(1) correction in a connected system.

    Correction was a tracked manual workflow because the engine has no
    correction action. That was honest but thin: the record of what happened was
    a sentence somebody typed, which is a claim rather than evidence. This makes
    the change and records what it actually changed — the old value, the new
    one, the row count, and who decided it.

    DRY RUN IS THE DEFAULT, and that is the opposite of `erase`.

    Erasure is preceded by discovery, so the admin has already seen what will be
    touched. A correction names one column and one new value, and the thing most
    likely to be wrong is the mapping between "Full name" on a form and
    `users.full_name` in a schema. So the first call shows what is there now and
    changes nothing; only an explicit `dry_run=False` writes. Both go down the
    same code path in `discovery.correct`, because a preview computed by
    different code from the write is worse than no preview.

    ONE COLUMN PER CALL, deliberately. A request that corrects a name in three
    systems is three decisions, each with its own receipt, not one bulk action
    whose failure halfway through leaves an unknown state.
    """
    request, principal = await _request_and_principal(session, request_id=request_id)

    if request.type not in ("correction", "completion", "updating"):
        raise CorrectionRefused(
            f"{request.reference} is a {request.type} request. Correcting data "
            "on the strength of one would be acting beyond what was asked."
        )

    if not (new_value or "").strip():
        raise CorrectionRefused(
            "A correction needs a new value. To remove a value rather than "
            "change it, the person is asking for erasure, which is a different "
            "right with different exemptions."
        )

    # Only the live write needs the reference typed back. Requiring it for a
    # preview would train people to type it without reading, which is precisely
    # what the guard exists to prevent.
    #
    # `_automatic` is the one exemption, and it is narrower than it looks: the
    # only caller is `auto_correct`, which reaches here solely when the person's
    # own statement of the current value matched exactly one column in exactly
    # one system. The guard protects against an unconsidered click, and there is
    # no click — the consideration happened when the request was verified and
    # the database agreed with what the requester said was in it.
    if not dry_run and not _automatic:
        if (confirm_reference or "").strip().upper() != request.reference.upper():
            raise CorrectionRefused(
                f"To apply this change, type the request's reference "
                f"({request.reference}) to confirm. This writes to a live "
                "system and there is no undo."
            )

    row = await session.scalar(
        select(Connection).where(Connection.id == connection_id)
    )
    if row is None:
        raise NotFound("No such connection.")
    if row.status != "connected":
        raise CorrectionRefused(
            f"{row.label} is not connected, so nothing can be written to it. "
            "Test the connection first."
        )

    try:
        config = {**row.config_public, **open_sealed(row.config_sealed)}
    except CredentialSealError as exc:
        raise CorrectionRefused(f"Could not read that connection: {exc}") from exc

    # Re-discovered rather than trusting a table name from the request body.
    # The finding carries what may be written — `would_mask` is the allowlist
    # `discovery.correct` enforces — and a stale one would let a column that is
    # no longer personal data be rewritten.
    identifiers = _identifiers(principal)
    found = await discovery.discover(row.connector_id, config, identifiers)
    if not found.ok:
        raise CorrectionRefused(
            found.error or "That system could not be searched just now."
        )

    finding = next((f for f in found.findings if f.table == table), None)
    if finding is None:
        raise CorrectionRefused(
            f"{table} does not hold anything matching this person, so there is "
            "nothing here to correct."
        )

    outcome = await discovery.correct(
        row.connector_id, config, finding,
        identifiers.get(finding.matched_identifier, ""),
        column, new_value, dry_run=dry_run,
    )

    if dry_run:
        # No event, no audit entry. Looking is not an act, and a timeline full
        # of "somebody previewed this" buries the line that says what changed.
        return {
            "request": request.reference,
            "connection": row.label,
            **outcome.as_dict(),
        }

    from app.services.dsar_service import _event

    how = "automatically" if _automatic else "by hand"
    if outcome.ok:
        note = (
            f"{row.label} · {table}.{column}: changed "
            f"{outcome.rows_affected} row(s) from "
            f"{', '.join(outcome.old_values) or '(empty)'} to {new_value} "
            f"({how})"
        )
    else:
        note = f"{row.label} · {table}.{column}: FAILED — {outcome.error}"

    await _event(
        session, tenant_id=tenant_id, request=request, actor=actor,
        note=note, automated=False,
    )

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.DSAR_CONNECTED_CORRECTION,
        entity_type="dsar_request", entity_id=request.id,
        payload={
            "reference": request.reference,
            "connection": row.label,
            "table": table,
            "column": column,
            # BOTH values. A correction recorded without the old one cannot
            # answer "changed from what?", which is the question an auditor
            # asks and the manual workflow could never answer.
            "old_values": outcome.old_values,
            "new_value": new_value,
            "rows_affected": outcome.rows_affected,
            "ok": outcome.ok,
            "error": outcome.error,
            # Which of the two happened. An automatic write had no human in
            # front of it, and an inquiry into a wrong correction starts by
            # asking exactly that — so it is a field, not something to infer
            # from the actor being a system account.
            "automatic": _automatic,
        },
    )

    return {
        "request": request.reference,
        "connection": row.label,
        **outcome.as_dict(),
    }


# --------------------------------------------------------------------------- #
# Automatic correction — §12(1) without a human hunting for the column
#
# `correct()` above does the writing, and expects somebody to have already
# worked out WHICH connection, table and column. That is the tedious half, and
# the half a person gets wrong: "Full name" on a rights form has to be matched
# to `users.full_name` in one schema and `customers.name` in another.
#
# WHAT MAKES THIS SAFE RATHER THAN A GUESS
#
# The request already states the CURRENT value — §12(1) asks what is wrong, so
# the person has told us what is there. That turns column matching from a guess
# into a verification: a column whose stored value equals what they said is
# almost certainly the cell they are talking about, and a column whose name
# looks right but whose value disagrees is almost certainly not.
#
# So a plan has two grades, and only one of them applies itself:
#
#   confirmed   the column name matches the requested field AND the stored
#               value equals the `current` the person stated. Unambiguous —
#               exactly one of these means there is nothing left to decide.
#   candidate   the name matches but the value does not, or several columns
#               match. A human picks, because picking is the actual judgement.
#
# Anything that is not a single confirmed match waits. Automation that resolves
# ambiguity by choosing is how the wrong person's name gets rewritten.
# --------------------------------------------------------------------------- #

import re as _re


def _normalise_field(name: str) -> str:
    """`Full name`, `full_name` and `fullName` are the same field."""
    return _re.sub(r"[^a-z0-9]", "", (name or "").lower())


#: Words a rights form uses for a column a schema names differently. Small and
#: explicit rather than a fuzzy-match library: a near-miss here rewrites the
#: wrong column, and "close enough" is not a standard to write to a production
#: database against.
_FIELD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "name": ("fullname", "name", "customername", "displayname"),
    "fullname": ("fullname", "name", "customername", "displayname"),
    "firstname": ("firstname", "givenname", "fname"),
    "lastname": ("lastname", "surname", "familyname", "lname"),
    "phone": ("phone", "phonenumber", "mobile", "mobilenumber", "contactnumber",
              "telephone", "msisdn"),
    "mobile": ("phone", "phonenumber", "mobile", "mobilenumber", "contactnumber"),
    "email": ("email", "emailaddress", "mail", "contactemail"),
    "address": ("address", "addressline1", "street", "postaladdress"),
    "city": ("city", "town"),
    "pincode": ("pincode", "postalcode", "zip", "zipcode", "postcode"),
    "dob": ("dob", "dateofbirth", "birthdate"),
}


def _field_matches(requested: str, column: str) -> bool:
    want = _normalise_field(requested)
    have = _normalise_field(column)
    if not want or not have:
        return False
    if want == have:
        return True
    return have in _FIELD_SYNONYMS.get(want, ())


async def plan_correction(
    session,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
) -> dict[str, Any]:
    """Work out where the requested change should land. Writes nothing.

    Reads every connected system, matches the requested field against the
    columns discovery classified as this person's personal data, and grades each
    hit against the `current` value the person stated.

    Returns the plan. `auto_applicable` is true only when exactly one confirmed
    match exists across every system — which is the one case with no judgement
    left in it.
    """
    request, principal = await _request_and_principal(session, request_id=request_id)

    if request.type not in ("correction", "completion", "updating"):
        raise CorrectionRefused(
            f"{request.reference} is a {request.type} request, so there is no "
            "correction to plan."
        )

    payload = request.correction_payload or {}
    # The public form nests it one level; the API takes it flat. Accept both
    # rather than making the shape a thing callers have to know.
    inner = payload.get("correction") if isinstance(payload.get("correction"), dict) else payload
    field = str(inner.get("field") or "").strip()
    stated_current = str(inner.get("current") or "").strip()
    corrected = str(inner.get("corrected") or "").strip()

    if not field or not corrected:
        raise CorrectionRefused(
            "This request does not say which field to change and what to. "
            "Without both, there is nothing to plan."
        )

    identifiers = _identifiers(principal)
    rows = (
        await session.execute(
            select(Connection).where(Connection.status == "connected")
        )
    ).scalars().all()

    confirmed: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    unreachable: list[dict[str, Any]] = []

    for row in rows:
        try:
            config = {**row.config_public, **open_sealed(row.config_sealed)}
        except CredentialSealError as exc:
            unreachable.append({"connection": row.label, "error": str(exc)})
            continue

        found = await discovery.discover(row.connector_id, config, identifiers)
        if not found.ok:
            unreachable.append({
                "connection": row.label,
                "error": found.error or "could not be searched",
            })
            continue

        for finding in found.findings:
            for column in finding.would_mask:
                if not _field_matches(field, column):
                    continue

                # A dry run, so this reads the stored value through exactly the
                # code path that would write it.
                probe = await discovery.correct(
                    row.connector_id, config, finding,
                    identifiers.get(finding.matched_identifier, ""),
                    column, corrected, dry_run=True,
                )
                if not probe.ok:
                    unreachable.append({
                        "connection": row.label, "table": finding.table,
                        "column": column, "error": probe.error,
                    })
                    continue

                hit = {
                    "connection_id": str(row.id),
                    "connection": row.label,
                    "table": finding.table,
                    "column": column,
                    "rows_matched": probe.rows_matched,
                    "current_values": probe.old_values,
                    "new_value": corrected,
                }

                # The verification, and it differs by type because the three
                # §12(1) rights make different claims about what is there now.
                #
                # completion says "this is MISSING". The checksum is therefore
                # that the stored value really is absent — not that it equals
                # something the person stated, because for completion there is
                # nothing to state. Without this branch, completion could never
                # confirm and every "add my phone number" waited for a human
                # forever, which is the same non-answer the manual workflow gave.
                #
                # correction and updating both say "this is WRONG, and here is
                # what it says". The stored value has to match that. Compared
                # case-insensitively and trimmed, because somebody retyping
                # their own name off a screen does not reproduce whitespace —
                # but not fuzzily beyond that.
                is_empty = all(not v.strip() for v in probe.old_values)

                if request.type == "completion":
                    agrees = is_empty
                    mismatch = (
                        "something is already stored here, so this is a "
                        "correction rather than a completion"
                    )
                else:
                    # BOTH sides trimmed here, not just the stored one.
                    # `stated_current` is already stripped upstream, and a
                    # comparison that decides whether to write to a production
                    # database should not depend on that having happened — a
                    # caller passing an untrimmed value would silently never
                    # match, sending every correction to a human with no
                    # indication why.
                    want = stated_current.strip().lower()
                    agrees = bool(want) and any(
                        v.strip().lower() == want for v in probe.old_values
                    )
                    mismatch = (
                        "the request did not say what the current value is"
                        if not stated_current
                        else "the stored value is not what the request says is there"
                    )

                if agrees and probe.rows_matched == 1:
                    confirmed.append(hit)
                else:
                    hit["why_not_confirmed"] = (
                        mismatch if not agrees
                        else f"{probe.rows_matched} rows match here, not one"
                    )
                    candidates.append(hit)

    return {
        "request": request.reference,
        "type": request.type,
        "field": field,
        "stated_current": stated_current,
        "new_value": corrected,
        "confirmed": confirmed,
        "candidates": candidates,
        "unreachable": unreachable,
        # The only case with no judgement left in it.
        "auto_applicable": len(confirmed) == 1 and not candidates,
        "why_not_automatic": (
            None if len(confirmed) == 1 and not candidates
            else "nothing in the connected systems matches this field and value"
            if not confirmed and not candidates
            else f"{len(confirmed)} confirmed and {len(candidates)} possible "
                 "match(es) — which one is right is a judgement, not a lookup"
        ),
    }


async def auto_correct(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    request_id: uuid.UUID,
) -> dict[str, Any]:
    """Plan, and carry it out when the plan leaves nothing to decide.

    This is what makes a correction request behave like an access or erasure
    one: it is confirmed, and then it happens. The difference is that access and
    erasure have no target to choose — a correction does, so this applies itself
    only when the person's own account of the current value and the database
    agree, in exactly one place.

    When they do not, the plan is returned and the request waits for a human.
    That is not a failure and is not reported as one: "two columns could be the
    one you mean" is a real answer, and choosing between them silently is the
    thing worth avoiding.
    """
    plan = await plan_correction(
        session, tenant_id=tenant_id, request_id=request_id
    )
    if not plan["auto_applicable"]:
        return {**plan, "applied": None}

    target = plan["confirmed"][0]
    result = await correct(
        session,
        tenant_id=tenant_id,
        actor=actor,
        request_id=request_id,
        connection_id=uuid.UUID(target["connection_id"]),
        table=target["table"],
        column=target["column"],
        new_value=plan["new_value"],
        # The reference guard exists so an irreversible act does not follow from
        # an unremarkable click. There is no click here — the act follows from a
        # verified request whose own account of the data the database confirmed,
        # and the audit entry records that it was automatic.
        confirm_reference=None,
        dry_run=False,
        _automatic=True,
    )
    return {**plan, "applied": result}
