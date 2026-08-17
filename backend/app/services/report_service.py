"""Compliance reporting. Every number comes from a query.

That sentence is the whole module. This codebase has already had to delete two
fabricated artifacts — a dashboard trend line invented from constants, and an
"export signature" computed from `rowCount * 7919`. Both looked like
measurements. On a product handed to a regulator a fabricated number is not a
cosmetic bug; it is the thing that destroys the product's reason to exist.

Consequences of that rule, all of them deliberate:

* **Nothing is stored.** No `report_runs` table. A stored report is a snapshot
  that can disagree with the data it came from, and when those two disagree in a
  compliance product you have a liability rather than a document. Reports are
  built by query and streamed.

* **An empty report is a legitimate report.** Zero rows renders as zero rows. It
  does not render as zeros dressed up as measurements, and it does not render a
  chart shape over an empty set.

* **Nothing is called signed.** Every report carries the audit chain head hash,
  which is honest and independently checkable against `POST /v1/audit/verify`.
  That is not a signature and the provenance block says so in those words. A key
  and key management would make it one; until then the label would be a lie.

* **Truncation is stated.** Silent truncation reads as completeness, so a report
  that hit a cap says which cap and by how much, in the provenance block, in
  every format.

The provenance block appears **twice** in a streamed export: a header before the
rows and a footer after them. Neither alone is enough. A header cannot state how
many rows were actually emitted; a footer is lost if the transfer dies halfway,
leaving a partial file that looks whole. Together, a truncated download is
detectable — the header promises a total the missing footer never confirms.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict
from app.models.audit import AuditAction, AuditEvent
from app.models.consent import Consent, DataPrincipal, Notice, Purpose
from app.models.dsar import DsarRequest
from app.models.grievance import Grievance
from app.models.publishable_key import ConsentProvenance
from app.models.retention import PurgeRun, RetentionPolicy
from app.models.tenant import Tenant
from app.services import audit_service
from app.services.audit_service import Actor

logger = logging.getLogger("app.reports")

# A five-year unfiltered audit extract is a denial of service somebody will
# trigger by accident. 366 days covers "the last financial year" — the longest
# period a DPO routinely needs — and anything longer is a deliberate request that
# should go through a person.
MAX_PERIOD_DAYS = 366

# Hard row ceiling per export. Reached rather than guessed: the point is that a
# report which hits it SAYS so, so the number matters less than the statement.
MAX_ROWS = 50_000

# How many rows the preview shows. The screen is for orientation; the export is
# the artifact.
PREVIEW_LIMIT = 50

# Rows fetched per round trip while streaming. Small enough that memory stays
# flat on a large export, large enough that a 50k-row report is not 50k queries.
STREAM_CHUNK = 500


class ReportRefused(Conflict):
    """A report that must not be generated as asked."""


# --------------------------------------------------------------------------- #
# Period handling
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime

    @property
    def days(self) -> int:
        return (self.end - self.start).days


def resolve_period(
    date_from: date | None, date_to: date | None
) -> Period:
    """Turn a pair of dates into a bounded, timezone-aware window.

    Defaults to the last 30 days. `date_to` is treated as inclusive — a DPO who
    asks for 1–31 March means the whole of the 31st, and an exclusive end would
    silently drop a day's activity from a statutory report.
    """
    now = datetime.now(UTC)
    end = (
        datetime.combine(date_to, datetime.min.time(), tzinfo=UTC) + timedelta(days=1)
        if date_to
        else now
    )
    start = (
        datetime.combine(date_from, datetime.min.time(), tzinfo=UTC)
        if date_from
        else end - timedelta(days=30)
    )
    if start >= end:
        raise ReportRefused("The start of the period must be before its end.")
    if (end - start).days > MAX_PERIOD_DAYS:
        raise ReportRefused(
            f"The period is limited to {MAX_PERIOD_DAYS} days "
            f"({(end - start).days} requested). Ask for a narrower window, or run "
            "several reports — an unbounded extract over the audit trail is how "
            "this API gets taken down by accident."
        )
    return Period(start=start, end=end)


# --------------------------------------------------------------------------- #
# The report catalogue
# --------------------------------------------------------------------------- #

@dataclass
class ReportDef:
    """One report: what it answers, and how to build it.

    `columns` is the contract. It fixes the order in CSV and the key order in
    JSON, so a customer's downstream parser does not break because somebody
    reordered a SELECT.
    """

    key: str
    title: str
    question: str
    columns: tuple[str, ...]
    # Which timestamp the period filters on. Named because it is a real choice:
    # a consent register filtered by `withdrawn_at` answers a different question
    # from one filtered by `given_at`, and getting it wrong produces a report
    # that is confidently about the wrong thing.
    period_column: str
    notes: str = ""
    caveats: tuple[str, ...] = field(default_factory=tuple)


REPORTS: dict[str, ReportDef] = {
    "consent_register": ReportDef(
        key="consent_register",
        title="Consent register",
        question="Who consented to what, under which notice version, when, and how.",
        columns=(
            "principal_ref", "principal_email", "purpose_key", "purpose_name",
            "category", "legal_basis", "status", "notice_version", "language",
            "method", "source", "given_at", "withdrawn_at", "expires_at",
            "collection_method", "strongly_bound", "origin", "server_receipt_id",
        ),
        period_column="given_at",
        notes="The register a regulator asks for first: the evidence that "
              "processing was permitted.",
        caveats=(
            "`strongly_bound` is false for consent collected from a banner without "
            "a signed token — the visitor identifier was asserted by the page, not "
            "verified by your server.",
        ),
    ),
    "consent_activity": ReportDef(
        key="consent_activity",
        title="Consent activity",
        question="Grants, withdrawals and expiries over a period.",
        columns=(
            "event", "occurred_at", "principal_ref", "purpose_key", "purpose_name",
            "category", "language", "method", "source",
        ),
        period_column="occurred_at",
        notes="One row per event, not per consent — a consent granted and later "
              "withdrawn inside the period appears twice, which is the point.",
    ),
    "dsar_register": ReportDef(
        key="dsar_register",
        title="Data request register",
        question="Every rights request, its type, status, and whether it met the "
                 "statutory deadline.",
        columns=(
            "reference", "type", "status", "principal_ref", "principal_email",
            "requested_by", "submitted_at", "deadline_at", "resolved_at",
            "days_taken", "met_deadline", "rejection_reason", "engine_ref",
        ),
        period_column="submitted_at",
        notes="`met_deadline` is computed per row: resolved on or before the "
              "deadline. Still-open requests report it as null rather than as a "
              "pass, because an unanswered request has not met anything yet.",
    ),
    "grievance_register": ReportDef(
        key="grievance_register",
        title="Grievance register",
        question="Every complaint, how long it took, and which were escalated.",
        columns=(
            "reference", "category", "status", "principal_ref", "contact_email",
            "contact_verified", "submitted_at", "deadline_at", "acknowledged_at",
            "resolved_at", "days_taken", "met_deadline", "escalated",
            "escalated_at", "satisfaction_rating",
        ),
        period_column="submitted_at",
        notes="DPDP §13. `contact_verified` matters: an unconfirmed anonymous "
              "complaint is recorded and counted but never escalates on its own.",
    ),
    "retention_purge": ReportDef(
        key="retention_purge",
        title="Retention and purge",
        question="Which policies are in force, which runs happened, and how many "
                 "rows each one touched.",
        columns=(
            "policy_name", "data_category", "retention_days", "action",
            "auto_delete", "exemption_code", "exemption_reference", "run_mode",
            "run_status", "started_at", "finished_at", "candidates_found",
            "rows_affected", "error",
        ),
        period_column="started_at",
        notes="Dry runs and live runs are both listed, distinguished by "
              "`run_mode`. A report that conflated them could not answer when "
              "data was actually destroyed.",
    ),
    "audit_extract": ReportDef(
        key="audit_extract",
        title="Audit extract",
        question="A slice of the tamper-evident chain, with each entry's hash "
                 "links intact.",
        columns=(
            "seq", "created_at", "action", "actor_type", "actor_label",
            "entity_type", "entity_id", "ip_address", "prev_hash", "hash",
        ),
        period_column="created_at",
        notes="Verify independently: recompute each hash from (tenant, seq, "
              "action, payload, prev_hash) and check the head against "
              "POST /v1/audit/verify.",
        caveats=(
            "The payload is deliberately omitted. Audit payloads contain personal "
            "data, and an export of the whole chain body is a second copy of it "
            "outside every control that governs the first. Read individual "
            "entries through /v1/audit when you need the detail.",
            "The chain detects entries being altered, removed or reordered. It "
            "cannot detect truncation of the newest entries — that needs external "
            "anchoring, which is not deployed.",
        ),
    ),
}


def get_definition(key: str) -> ReportDef:
    try:
        return REPORTS[key]
    except KeyError:
        raise ReportRefused(
            f"Unknown report {key!r}. Available: {', '.join(sorted(REPORTS))}."
        ) from None


# --------------------------------------------------------------------------- #
# Row builders — one query each, no post-hoc arithmetic on invented values
# --------------------------------------------------------------------------- #

def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


def _days_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return (end - start).days


def _consent_register_stmt(tenant_id: uuid.UUID, period: Period) -> Select:
    return (
        select(Consent, Purpose, DataPrincipal, Notice, ConsentProvenance)
        .join(Purpose, Purpose.id == Consent.purpose_id)
        .join(DataPrincipal, DataPrincipal.id == Consent.principal_id)
        .outerjoin(Notice, Notice.id == Consent.notice_id)
        # Outer: provenance exists only for records collected through the public
        # banner. An inner join here would silently drop every consent captured
        # in the console — which is most of them.
        .outerjoin(ConsentProvenance, ConsentProvenance.consent_id == Consent.id)
        .where(
            Consent.tenant_id == tenant_id,
            Consent.given_at >= period.start,
            Consent.given_at < period.end,
        )
        .order_by(Consent.given_at.desc())
    )


def _consent_register_row(row: Any) -> dict[str, Any]:
    consent, purpose, principal, notice, prov = row
    return {
        "principal_ref": principal.external_id,
        "principal_email": principal.email,
        "purpose_key": purpose.key,
        "purpose_name": purpose.name,
        "category": purpose.category,
        "legal_basis": purpose.legal_basis,
        "status": consent.status,
        "notice_version": notice.version if notice else None,
        "language": consent.language,
        "method": consent.method,
        "source": consent.source,
        "given_at": _iso(consent.given_at),
        "withdrawn_at": _iso(consent.withdrawn_at),
        "expires_at": _iso(consent.expires_at),
        "collection_method": prov.collection_method if prov else None,
        # Reported as-is. False means the visitor identifier was asserted by a
        # page rather than verified by a server, and a register that hid that
        # would overstate the strength of its own evidence.
        "strongly_bound": prov.strongly_bound if prov else None,
        "origin": prov.origin if prov else None,
        "server_receipt_id": prov.server_receipt_id if prov else None,
    }


def _consent_activity_stmt(tenant_id: uuid.UUID, period: Period) -> Select:
    """Grants and withdrawals as separate events, unioned.

    Two SELECTs over the same table rather than one row per consent, because
    "activity in this period" is a question about events. A consent granted in
    March and withdrawn in March is two things that happened.
    """
    granted = (
        select(
            literal("granted").label("event"),
            Consent.given_at.label("occurred_at"),
            DataPrincipal.external_id.label("principal_ref"),
            Purpose.key.label("purpose_key"),
            Purpose.name.label("purpose_name"),
            Purpose.category.label("category"),
            Consent.language.label("language"),
            Consent.method.label("method"),
            Consent.source.label("source"),
        )
        .join(Purpose, Purpose.id == Consent.purpose_id)
        .join(DataPrincipal, DataPrincipal.id == Consent.principal_id)
        .where(
            Consent.tenant_id == tenant_id,
            Consent.given_at >= period.start,
            Consent.given_at < period.end,
        )
    )
    withdrawn = (
        select(
            literal("withdrawn").label("event"),
            Consent.withdrawn_at.label("occurred_at"),
            DataPrincipal.external_id.label("principal_ref"),
            Purpose.key.label("purpose_key"),
            Purpose.name.label("purpose_name"),
            Purpose.category.label("category"),
            Consent.language.label("language"),
            Consent.method.label("method"),
            Consent.source.label("source"),
        )
        .join(Purpose, Purpose.id == Consent.purpose_id)
        .join(DataPrincipal, DataPrincipal.id == Consent.principal_id)
        .where(
            Consent.tenant_id == tenant_id,
            Consent.withdrawn_at.isnot(None),
            Consent.withdrawn_at >= period.start,
            Consent.withdrawn_at < period.end,
        )
    )
    union = granted.union_all(withdrawn).subquery()
    return select(union).order_by(union.c.occurred_at.desc())


def _consent_activity_row(row: Any) -> dict[str, Any]:
    return {
        "event": row.event,
        "occurred_at": _iso(row.occurred_at),
        "principal_ref": row.principal_ref,
        "purpose_key": row.purpose_key,
        "purpose_name": row.purpose_name,
        "category": row.category,
        "language": row.language,
        "method": row.method,
        "source": row.source,
    }


def _dsar_register_stmt(tenant_id: uuid.UUID, period: Period) -> Select:
    return (
        select(DsarRequest, DataPrincipal)
        .join(DataPrincipal, DataPrincipal.id == DsarRequest.principal_id)
        .where(
            DsarRequest.tenant_id == tenant_id,
            DsarRequest.submitted_at >= period.start,
            DsarRequest.submitted_at < period.end,
        )
        .order_by(DsarRequest.submitted_at.desc())
    )


def _dsar_register_row(row: Any) -> dict[str, Any]:
    request, principal = row
    resolved = request.resolved_at
    return {
        "reference": request.reference,
        "type": request.type,
        "status": request.status,
        "principal_ref": principal.external_id,
        "principal_email": principal.email,
        "requested_by": request.requested_by_actor,
        "submitted_at": _iso(request.submitted_at),
        "deadline_at": _iso(request.deadline_at),
        "resolved_at": _iso(resolved),
        "days_taken": _days_between(request.submitted_at, resolved),
        # Null, not True, while a request is still open. An unanswered request
        # has not met a deadline; reporting it as a pass is how an SLA figure
        # ends up flattering.
        "met_deadline": (resolved <= request.deadline_at) if resolved else None,
        "rejection_reason": request.rejection_reason,
        "engine_ref": request.engine_ref,
    }


def _grievance_register_stmt(tenant_id: uuid.UUID, period: Period) -> Select:
    return (
        select(Grievance, DataPrincipal)
        .outerjoin(DataPrincipal, DataPrincipal.id == Grievance.principal_id)
        .where(
            Grievance.tenant_id == tenant_id,
            Grievance.submitted_at >= period.start,
            Grievance.submitted_at < period.end,
        )
        .order_by(Grievance.submitted_at.desc())
    )


def _grievance_register_row(row: Any) -> dict[str, Any]:
    grievance, principal = row
    resolved = grievance.resolved_at
    return {
        "reference": grievance.reference,
        "category": grievance.category,
        "status": grievance.status,
        "principal_ref": principal.external_id if principal else None,
        "contact_email": grievance.contact_email,
        "contact_verified": grievance.contact_verified,
        "submitted_at": _iso(grievance.submitted_at),
        "deadline_at": _iso(grievance.deadline_at),
        "acknowledged_at": _iso(grievance.acknowledged_at),
        "resolved_at": _iso(resolved),
        "days_taken": _days_between(grievance.submitted_at, resolved),
        "met_deadline": (resolved <= grievance.deadline_at) if resolved else None,
        "escalated": grievance.escalated,
        "escalated_at": _iso(grievance.escalated_at),
        "satisfaction_rating": grievance.satisfaction_rating,
    }


def _retention_purge_stmt(tenant_id: uuid.UUID, period: Period) -> Select:
    return (
        select(PurgeRun, RetentionPolicy)
        .join(RetentionPolicy, RetentionPolicy.id == PurgeRun.policy_id)
        .where(
            PurgeRun.tenant_id == tenant_id,
            PurgeRun.started_at >= period.start,
            PurgeRun.started_at < period.end,
        )
        .order_by(PurgeRun.started_at.desc())
    )


def _retention_purge_row(row: Any) -> dict[str, Any]:
    run, policy = row
    return {
        "policy_name": policy.name,
        "data_category": policy.data_category,
        "retention_days": policy.retention_days,
        "action": policy.action,
        "auto_delete": policy.auto_delete,
        "exemption_code": policy.exemption_code,
        "exemption_reference": policy.exemption_reference,
        "run_mode": run.mode,
        "run_status": run.status,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "candidates_found": run.candidates_found,
        "rows_affected": run.rows_affected,
        "error": run.error,
    }


def _audit_extract_stmt(tenant_id: uuid.UUID, period: Period) -> Select:
    return (
        select(AuditEvent)
        .where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.created_at >= period.start,
            AuditEvent.created_at < period.end,
        )
        # Ascending: the chain only reads as a chain in sequence order, and a
        # reader checking prev_hash links needs them in the order they were made.
        .order_by(AuditEvent.seq.asc())
    )


def _audit_extract_row(row: Any) -> dict[str, Any]:
    event = row[0]
    return {
        "seq": event.seq,
        "created_at": _iso(event.created_at),
        "action": event.action,
        "actor_type": event.actor_type,
        "actor_label": event.actor_label,
        "entity_type": event.entity_type,
        "entity_id": str(event.entity_id) if event.entity_id else None,
        "ip_address": event.ip_address,
        "prev_hash": event.prev_hash,
        "hash": event.hash,
        # payload deliberately absent — see the definition's caveat.
    }


_BUILDERS: dict[str, tuple[Any, Any]] = {
    "consent_register": (_consent_register_stmt, _consent_register_row),
    "consent_activity": (_consent_activity_stmt, _consent_activity_row),
    "dsar_register": (_dsar_register_stmt, _dsar_register_row),
    "grievance_register": (_grievance_register_stmt, _grievance_register_row),
    "retention_purge": (_retention_purge_stmt, _retention_purge_row),
    "audit_extract": (_audit_extract_stmt, _audit_extract_row),
}


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #

@dataclass
class Provenance:
    """What a reader needs in order to trust, or distrust, the rows above.

    `chain_verified` has three states and the third one matters. Verifying walks
    the tenant's entire chain, which is too expensive to do on every report, so
    the default is an honest "not checked" rather than a cheap "OK" nobody
    earned.
    """

    report: str
    title: str
    generated_at: str
    generated_by: str
    workspace: str
    workspace_slug: str
    period_from: str
    period_to: str
    total_matching: int
    row_limit: int
    truncated: bool
    chain_head_hash: str | None
    chain_head_seq: int | None
    chain_verified: str  # "ok" | "failed" | "not_checked"
    chain_problem: str | None
    caveats: list[str]
    # Set after streaming finishes. Absent from the header block by necessity —
    # you cannot count rows you have not emitted yet — which is why the footer
    # exists at all.
    rows_in_report: int | None = None

    def as_lines(self) -> list[str]:
        """Human-readable, for the CSV comment blocks and the on-screen panel."""
        signed_note = (
            "This report is NOT digitally signed. The chain head hash above is a "
            "tamper-evidence anchor you can recompute yourself; it is not a "
            "signature and does not prove who generated this file."
        )
        lines = [
            f"Report              {self.title} ({self.report})",
            f"Generated at        {self.generated_at}",
            f"Generated by        {self.generated_by}",
            f"Workspace           {self.workspace} ({self.workspace_slug})",
            f"Period covered      {self.period_from} .. {self.period_to}",
            f"Rows matching       {self.total_matching}",
        ]
        if self.rows_in_report is not None:
            lines.append(f"Rows in this report {self.rows_in_report}")
        if self.truncated:
            lines.append(
                f"TRUNCATED           yes — capped at {self.row_limit} rows of "
                f"{self.total_matching}. Narrow the period to get the rest."
            )
        lines += [
            f"Audit chain head    {self.chain_head_hash or '(no entries)'}"
            + (f" (seq {self.chain_head_seq})" if self.chain_head_seq else ""),
            f"Chain verified      {self.chain_verified}"
            + (f" — {self.chain_problem}" if self.chain_problem else ""),
            "Verify              POST /v1/audit/verify",
            f"Not a signature     {signed_note}",
        ]
        for caveat in self.caveats:
            lines.append(f"Caveat              {caveat}")
        return lines


async def build_provenance(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    definition: ReportDef,
    period: Period,
    generated_by: str,
    total: int,
    row_limit: int,
    verify_chain: bool,
) -> Provenance:
    tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))

    head_hash: str | None = None
    head_seq: int | None = None
    verified = "not_checked"
    problem: str | None = None

    if verify_chain:
        status = await audit_service.verify_chain(session, tenant_id=tenant_id)
        head_hash, head_seq = status.head_hash, status.head_seq
        verified = "ok" if status.ok else "failed"
        problem = status.problem
    else:
        # The head alone is one indexed row, so it is cheap enough to include
        # always. Including it without claiming it was verified is the honest
        # middle: a reader can check it themselves.
        head = await session.execute(
            select(AuditEvent.seq, AuditEvent.hash)
            .where(AuditEvent.tenant_id == tenant_id)
            .order_by(AuditEvent.seq.desc())
            .limit(1)
        )
        row = head.first()
        if row:
            head_seq, head_hash = row[0], row[1]

    return Provenance(
        report=definition.key,
        title=definition.title,
        generated_at=datetime.now(UTC).isoformat(),
        generated_by=generated_by,
        workspace=tenant.name if tenant else "(unknown)",
        workspace_slug=tenant.slug if tenant else "(unknown)",
        period_from=period.start.isoformat(),
        period_to=period.end.isoformat(),
        total_matching=total,
        row_limit=row_limit,
        truncated=total > row_limit,
        chain_head_hash=head_hash,
        chain_head_seq=head_seq,
        chain_verified=verified,
        chain_problem=problem,
        caveats=list(definition.caveats),
    )


# --------------------------------------------------------------------------- #
# Counting and previewing
# --------------------------------------------------------------------------- #

async def count_rows(
    session: AsyncSession, *, tenant_id: uuid.UUID, key: str, period: Period
) -> int:
    """How many rows match, wrapped around the SAME statement the export streams.

    A hand-written parallel count query is how a report claims 4 rows and streams
    400 — the trap the purge executor avoids by sharing one candidate-selection
    path. So this wraps the real statement in a subquery rather than restating its
    filters.

    Counting via `with_only_columns(func.count())` was the obvious approach and is
    wrong here: replacing the projection also drops the inferred FROM when the
    statement selects from a subquery, as the activity report does, and the query
    silently degrades to `SELECT count(*)` — which returns 1, always, including for
    an empty period. Wrapping keeps the FROM whatever shape the statement had.
    """
    stmt, _ = _BUILDERS[key]
    inner = stmt(tenant_id, period).order_by(None).subquery()
    return (await session.scalar(select(func.count()).select_from(inner))) or 0


async def preview(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    key: str,
    period: Period,
    generated_by: str,
    offset: int = 0,
    limit: int = PREVIEW_LIMIT,
    verify_chain: bool = False,
) -> dict[str, Any]:
    """A page of rows plus the same provenance the export carries.

    The provenance is on the screen deliberately. The person reading the screen
    is making the same decisions as the person reading the file, and a caveat
    that only appears in the download is a caveat most people never see.
    """
    definition = get_definition(key)
    stmt, to_row = _BUILDERS[key]
    total = await count_rows(session, tenant_id=tenant_id, key=key, period=period)

    result = await session.execute(stmt(tenant_id, period).offset(offset).limit(limit))
    rows = [to_row(r) for r in result.all()]

    prov = await build_provenance(
        session, tenant_id=tenant_id, definition=definition, period=period,
        generated_by=generated_by, total=total, row_limit=MAX_ROWS,
        verify_chain=verify_chain,
    )
    prov.rows_in_report = len(rows)
    return {
        "report": definition.key,
        "title": definition.title,
        "question": definition.question,
        "columns": list(definition.columns),
        "rows": rows,
        "total": total,
        "offset": offset,
        "limit": limit,
        "provenance": prov,
        "provenance_lines": prov.as_lines(),
    }


# --------------------------------------------------------------------------- #
# Streaming exports
# --------------------------------------------------------------------------- #

async def _iter_rows(
    session: AsyncSession, *, tenant_id: uuid.UUID, key: str, period: Period
) -> AsyncIterator[dict[str, Any]]:
    """Yield rows in bounded chunks, never the whole set at once.

    Keyset pagination is not available for every one of these (the activity
    report is a union), so this uses OFFSET — which is fine at these row counts
    and bounded by MAX_ROWS. What matters is that memory stays flat: the whole
    result is never materialised, which is the difference between a large report
    and an outage.
    """
    stmt, to_row = _BUILDERS[key]
    base = stmt(tenant_id, period)
    emitted = 0
    offset = 0
    while emitted < MAX_ROWS:
        take = min(STREAM_CHUNK, MAX_ROWS - emitted)
        result = await session.execute(base.offset(offset).limit(take))
        batch = result.all()
        if not batch:
            return
        for raw in batch:
            yield to_row(raw)
            emitted += 1
        offset += len(batch)
        if len(batch) < take:
            return


def _csv_line(values: Sequence[Any]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["" if v is None else v for v in values])
    return buf.getvalue()


async def stream_csv(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    key: str,
    period: Period,
    generated_by: str,
    verify_chain: bool = False,
) -> AsyncIterator[str]:
    """CSV with a `#` provenance block before and after the rows.

    Both blocks, for the reason in the module docstring: the header cannot state
    how many rows were emitted, and the footer is lost if the transfer dies. A
    file whose header promises a total and whose footer is missing is visibly
    incomplete, which is the property worth having.
    """
    definition = get_definition(key)
    total = await count_rows(session, tenant_id=tenant_id, key=key, period=period)
    prov = await build_provenance(
        session, tenant_id=tenant_id, definition=definition, period=period,
        generated_by=generated_by, total=total, row_limit=MAX_ROWS,
        verify_chain=verify_chain,
    )

    for line in prov.as_lines():
        yield f"# {line}\n"
    yield "#\n"
    yield _csv_line(definition.columns)

    count = 0
    async for row in _iter_rows(session, tenant_id=tenant_id, key=key, period=period):
        yield _csv_line([row.get(c) for c in definition.columns])
        count += 1

    prov.rows_in_report = count
    yield "#\n"
    yield f"# END OF REPORT — {count} row(s) emitted\n"
    for line in prov.as_lines():
        yield f"# {line}\n"


async def stream_json(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    key: str,
    period: Period,
    generated_by: str,
    verify_chain: bool = False,
) -> AsyncIterator[str]:
    """NDJSON-framed inside a JSON envelope, provenance first.

    Provenance leads so it survives a partial transfer, and `rows_in_report` is
    repeated at the end once it is known. A parser that reaches `complete: true`
    knows it has the whole thing.
    """
    definition = get_definition(key)
    total = await count_rows(session, tenant_id=tenant_id, key=key, period=period)
    prov = await build_provenance(
        session, tenant_id=tenant_id, definition=definition, period=period,
        generated_by=generated_by, total=total, row_limit=MAX_ROWS,
        verify_chain=verify_chain,
    )

    head = {
        "report": definition.key,
        "title": definition.title,
        "question": definition.question,
        "columns": list(definition.columns),
        "provenance": prov.__dict__ | {"rows_in_report": None},
        "not_a_signature": (
            "The chain head hash is a tamper-evidence anchor, not a signature."
        ),
    }
    yield "{" + f'"meta": {json.dumps(head)}, "rows": ['

    count = 0
    async for row in _iter_rows(session, tenant_id=tenant_id, key=key, period=period):
        yield ("," if count else "") + json.dumps(row, default=str)
        count += 1

    yield "], " + json.dumps(
        {"rows_in_report": count, "truncated": prov.truncated, "complete": True}
    )[1:]


async def record_generation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    definition: ReportDef,
    period: Period,
    fmt: str,
) -> None:
    """Audit the extract before streaming it.

    Recorded first, not last: the stream runs on its own session and may fail
    halfway, and "somebody asked for the whole consent register" is the fact worth
    keeping either way. An entry written only on success would miss exactly the
    attempts worth reviewing.
    """
    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action=AuditAction.REPORT_GENERATED,
        entity_type="report",
        entity_id=None,
        payload={
            "report": definition.key,
            "format": fmt,
            "period_from": period.start.isoformat(),
            "period_to": period.end.isoformat(),
        },
    )


async def stream_report(
    *,
    tenant_id: uuid.UUID,
    key: str,
    period: Period,
    generated_by: str,
    fmt: str,
    verify_chain: bool = False,
) -> AsyncIterator[str]:
    """Stream an export on a session this generator owns.

    **Why its own session.** A `StreamingResponse` body is consumed after the
    route handler has returned, so the request-scoped session may already be
    closed by the time the second chunk is pulled. Borrowing it would work in
    development and fail under load, which is the worst kind of bug. The generator
    opens a session, binds tenant context so RLS applies exactly as it does to any
    other read, and closes it when the last chunk is out.
    """
    from app.db.session import get_session_factory, set_tenant_context

    async with get_session_factory()() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_id)
            inner = stream_csv if fmt == "csv" else stream_json
            async for chunk in inner(
                session, tenant_id=tenant_id, key=key, period=period,
                generated_by=generated_by, verify_chain=verify_chain,
            ):
                yield chunk


def filename_for(key: str, fmt: str, period: Period) -> str:
    return (
        f"{key}_{period.start.date().isoformat()}_{period.end.date().isoformat()}.{fmt}"
    )
