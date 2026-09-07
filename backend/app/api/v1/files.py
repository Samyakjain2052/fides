"""Download route for stored objects.

Uploads live with the thing being uploaded to — an identity document is posted
to its rights request, an attachment to its message — because the entity is what
decides whether the upload is allowed at all. Downloads are shared, so they are
here.

THREE HEADERS, AND WHY EACH IS NOT OPTIONAL

`Content-Disposition: attachment` — never inline. A PDF or an image rendered in
the browser from our own origin is a scripting surface, and the one class of
file we most want to accept (a photographed ID, a customer's CSV) is exactly the
one an attacker will try to make executable.

`X-Content-Type-Options: nosniff` — because a browser that disbelieves our
Content-Type and guesses again defeats the detection in `file_service`.

`Cache-Control: private, no-store` — these are identity documents and assembled
disclosure packages. A shared cache holding one is a breach with extra steps.
"""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select

from app.api.deps import CurrentUser, get_current_user
from app.models.consent import DataPrincipal
from app.services import file_service

router = APIRouter(prefix="/files", tags=["files"])


async def _principal_ids(current: CurrentUser) -> set[uuid.UUID]:
    """The Data Principal records this signed-in person *is*.

    Staff and principals are different tables, and a console user gets a
    principal row the first time they exercise a right of their own. Matching on
    both the synthetic `user:<id>` key and the email address covers the case
    where a customer's own import created the record first.
    """
    rows = (
        await current.session.execute(
            select(DataPrincipal.id).where(
                (DataPrincipal.external_id == f"user:{current.user.id}")
                | (DataPrincipal.email == current.user.email)
            )
        )
    ).scalars().all()
    return set(rows)


@router.get("/{file_id}", summary="Download a stored file")
async def download(
    file_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(get_current_user)],
) -> Response:
    """Fetch one file, if this caller may read it for its purpose.

    No capability is declared on the route itself, because the required
    capability depends on what the file is for — see `file_service.assert_readable`.
    A single `dsar:read` gate here would either lock data principals out of their
    own disclosure package or let an auditor open somebody's passport.

    Every refusal is 404, never 403. "This file exists but you may not have it"
    confirms the existence of a document about a named person to somebody who
    should not know it is there.
    """
    row, data = await file_service.fetch(current.session, file_id=file_id)
    await file_service.assert_readable(
        current.session,
        row,
        capabilities=set(current.capabilities),
        principal_ids=await _principal_ids(current),
    )

    # RFC 5987 for the filename: these come from customers and will contain
    # spaces, quotes and non-ASCII. `filename*` is understood everywhere that
    # matters; the plain `filename` stays as a conservative fallback.
    ascii_name = row.filename.encode("ascii", "replace").decode("ascii")
    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(row.filename)}"
    )

    return Response(
        content=data,
        media_type=row.content_type,
        headers={
            "Content-Disposition": disposition,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            # Belt and braces: even served as an attachment, a document that
            # somehow renders must not be able to run anything or reach out.
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
