"""Report routes — a catalogue, a paginated preview, and a streamed export.

Three things worth stating up front:

* **CSV and JSON only.** PDF is deferred, so there is no PDF option here and no
  PDF button on the screen. A dead control that returns "coming soon" is worse
  than an absent one: it makes a customer plan around a capability that does not
  exist.

* **Exports stream.** `StreamingResponse` over an async generator that pulls rows
  in bounded chunks. Building the whole result in memory and then serialising it
  is how a large report becomes an outage, and reports run over the biggest tables
  in the system.

* **The Auditor role reaches reports and audit, and nothing else.** The nav
  already reflects that; these routes enforce it independently, because nav is
  presentation. A test signs in as an auditor and asserts 403 on consent write,
  DSAR processing, users and retention.

Nothing here is labelled signed. See `report_service` for why, and for what the
chain head hash actually gives you.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, require
from app.core.permissions import Capability
from app.services import report_service

router = APIRouter(prefix="/reports", tags=["reports"])

# CSV and JSON. Deliberately not a superset that includes a format we cannot
# produce — see the module docstring.
FORMATS = ("csv", "json")


class ReportTypeOut(BaseModel):
    key: str
    title: str
    question: str
    columns: list[str]
    period_column: str
    notes: str
    caveats: list[str]


class CatalogueOut(BaseModel):
    reports: list[ReportTypeOut]
    formats: list[str]
    max_period_days: int
    max_rows: int
    # Stated in the API, not only in prose, so an integrator cannot mistake the
    # chain-hash anchor for a signature.
    signing: str


class ProvenanceOut(BaseModel):
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
    chain_verified: str
    chain_problem: str | None
    caveats: list[str]
    rows_in_report: int | None


class PreviewOut(BaseModel):
    report: str
    title: str
    question: str
    columns: list[str]
    rows: list[dict[str, Any]]
    total: int
    offset: int
    limit: int
    provenance: ProvenanceOut
    # The same block the export carries, pre-rendered. The screen shows it
    # verbatim so the person reading the page sees exactly what the person
    # reading the file sees.
    provenance_lines: list[str]


@router.get("", response_model=CatalogueOut, summary="Available reports and their limits")
async def catalogue(
    current: Annotated[CurrentUser, Depends(require(Capability.REPORT_GENERATE))],
) -> Any:
    return CatalogueOut(
        reports=[
            ReportTypeOut(
                key=d.key, title=d.title, question=d.question,
                columns=list(d.columns), period_column=d.period_column,
                notes=d.notes, caveats=list(d.caveats),
            )
            for d in report_service.REPORTS.values()
        ],
        formats=list(FORMATS),
        max_period_days=report_service.MAX_PERIOD_DAYS,
        max_rows=report_service.MAX_ROWS,
        signing=(
            "Reports are NOT digitally signed. Each carries the audit chain head "
            "hash, which you can recompute and check against POST /v1/audit/verify. "
            "That is tamper evidence, not a signature, and it does not prove who "
            "generated a given file."
        ),
    )


@router.get("/{report_key}/preview", response_model=PreviewOut,
            summary="A page of the report, with its provenance block")
async def preview(
    report_key: str,
    current: Annotated[CurrentUser, Depends(require(Capability.REPORT_GENERATE))],
    date_from: date | None = None,
    date_to: date | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(report_service.PREVIEW_LIMIT, ge=1, le=500),
    verify_chain: bool = Query(
        False,
        description="Recompute the whole audit chain and report the result in the "
                    "provenance block. Off by default because it walks every entry; "
                    "left off, the block honestly says 'not_checked' rather than "
                    "implying a check nobody ran.",
    ),
) -> Any:
    report_service.get_definition(report_key)
    period = report_service.resolve_period(date_from, date_to)
    return await report_service.preview(
        current.session,
        tenant_id=current.tenant_id,
        key=report_key,
        period=period,
        generated_by=current.user.email,
        offset=offset,
        limit=limit,
        verify_chain=verify_chain,
    )


class GenerateBody(BaseModel):
    date_from: date | None = None
    date_to: date | None = Field(
        None, description="Inclusive. Asking for 1–31 March covers the whole of "
                          "the 31st."
    )
    format: str = Field("csv", pattern="^(csv|json)$")
    verify_chain: bool = False


@router.post("/{report_key}/generate", summary="Stream the report as CSV or JSON")
async def generate(
    report_key: str,
    body: GenerateBody,
    current: Annotated[CurrentUser, Depends(require(Capability.REPORT_GENERATE))],
) -> StreamingResponse:
    """Stream, never store.

    Nothing is written to disk or to a `report_runs` table. A stored report is a
    snapshot that can disagree with the data it came from, and in a compliance
    product that disagreement is a liability rather than a document.

    The generation itself is audited — who asked for what, over which period —
    because "who extracted the consent register last quarter" is a question worth
    being able to answer about a file full of personal data.
    """
    definition = report_service.get_definition(report_key)
    period = report_service.resolve_period(body.date_from, body.date_to)

    await report_service.record_generation(
        current.session,
        tenant_id=current.tenant_id,
        actor=current.actor,
        definition=definition,
        period=period,
        fmt=body.format,
    )

    # The stream opens its own session. A StreamingResponse body is consumed
    # after this handler returns, so the request-scoped session may already be
    # closed by the second chunk — a bug that works in development and fails
    # under load. See report_service.stream_report.
    stream = report_service.stream_report(
        tenant_id=current.tenant_id,
        key=report_key,
        period=period,
        generated_by=current.user.email,
        fmt=body.format,
        verify_chain=body.verify_chain,
    )
    media = "text/csv; charset=utf-8" if body.format == "csv" else "application/json"

    filename = report_service.filename_for(report_key, body.format, period)
    return StreamingResponse(
        stream,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # No Content-Length: the size is not known until the last row is
            # emitted, and guessing one would break the download rather than
            # improve it.
            "Cache-Control": "no-store",
        },
    )
