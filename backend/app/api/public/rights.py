"""Public rights-request intake — no account required, and no credential.

The same argument the public grievance endpoint already makes, applied to §11
and §12. A person whose data a company holds may have no account with them at
all: somebody whose number was bought from a broker, a former customer whose
login was closed years ago, or somebody asking for erasure precisely *because*
they never signed up. Requiring an account to exercise a statutory right puts a
barrier in front of the right, and the people most likely to need it are the
ones least likely to have an account.

Until this existed, the only way in was `/user/dsar` behind a login. That made
the rights portal work for existing customers and useless for everybody else.

WHY NOT A PUBLISHABLE KEY

The banner API's keys are capped at `consent:collect`, and that ceiling is
enforced at issue, in the service, and by a CHECK constraint — which is what
makes "this key cannot do harm" true rather than aspirational. Widening it to
cover rights requests would trade a strong, testable property for convenience,
and it would only work on pages the customer had instrumented. So this stands on
its own, exactly as grievance filing does.

WHAT REPLACES THE CREDENTIAL

1. **The address must be confirmed before anything executes.** The request is
   recorded, counted and visible in the DPO's queue immediately — a statutory
   clock a company can stop by ignoring an email is not a clock — but it is not
   dispatched to the engine and no data moves until somebody proves they control
   the mailbox. That ordering is the whole safety property: an unconfirmed
   erasure request must never delete anything.

2. **Throttles built from data already on the table**, not from stored client
   IPs. Logging the IP of everybody who exercises a privacy right, in order to
   protect the privacy-rights system, is a poor trade.

3. **The workspace is addressed by slug**, which is already public — it is in
   the sign-in URL. Nothing here is enumerable that a login page does not
   already reveal, and an unknown slug gets a deliberately vague refusal.

WHAT IT CANNOT DO

Read a request, list them, or return anything about one that already exists.
Status tracking needs the account portal or the emailed link: a status endpoint
keyed on a guessable reference would leak who has asked a company to delete
their data, which is itself sensitive.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UnscopedSession
from app.core.config import get_settings
from app.core.errors import Conflict, NotFound, RateLimited
from app.db.session import get_session_factory, set_tenant_context
from app.models.consent import DataPrincipal
from app.models.dsar import DsarRequest
from app.models.tenant import Tenant
from app.services import dsar_service
from app.services.audit_service import Actor

router = APIRouter(prefix="/public/v1/rights", tags=["public API — rights"])

#: Ceiling per workspace per hour on anonymous intake.
#:
#: Sized for a real site's traffic rather than for an API client. A company
#: receiving more than this in an hour is either very large — in which case the
#: authenticated portal is the right route — or being used to generate work, and
#: the second is what this number is for.
MAX_PER_TENANT_PER_HOUR = 40


class PublicRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(
        ..., min_length=2, max_length=63,
        description="The organisation's workspace id — the same one used at "
                    "sign-in.",
    )
    #: All five §11/§12 rights. Nomination is absent on purpose: it is a
    #: standing arrangement about the future rather than a request, and one made
    #: through an unverified public form by somebody claiming to be a named
    #: person would be an obvious route to hijacking that person's rights.
    type: str = Field(..., examples=["access", "erasure"])
    email: EmailStr = Field(
        ...,
        description="Required. Without an account there is no other way to "
                    "confirm the request is genuine or to return the answer.",
    )
    name: str | None = Field(None, max_length=200)
    phone: str | None = Field(None, max_length=32)
    #: Required for correction, completion and updating.
    details: dict[str, Any] | None = None


class PublicAccepted(BaseModel):
    """Deliberately thin.

    The reference and the deadline, and nothing that could be used to probe for
    other people's requests. Notably absent: any id, and any indication of
    whether this address has asked before — which would turn the endpoint into a
    "does this person have an account here" oracle.
    """

    reference: str
    deadline_at: Any
    confirmation_required: bool
    message: str


def _confirm_url(*, workspace: str, reference_hint: str) -> str:
    """Where the confirmation link points.

    Built from `public_base_url`, never from the incoming request's host. That
    is not a style preference: invitation links were once built from
    `request.base_url` and, behind Container Apps' internal ingress, went out
    pointing at an unreachable internal FQDN. Every emailed link in this product
    now comes from configuration for that reason.
    """
    base = (get_settings().public_base_url or "").rstrip("/")
    if not base:
        # No base URL configured — send a relative path rather than a link to
        # nowhere. In production `public_base_url` is required, so this is the
        # development case.
        base = ""
    return (
        f"{base}/confirm-request"
        f"?workspace={quote(workspace)}&token={quote(reference_hint)}"
    )


async def _tenant_by_slug(session: AsyncSession, workspace: str) -> Tenant:
    """Resolve the workspace before tenant context exists.

    `tenants` has no RLS policy, so this is one of the handful of legitimate
    pre-context lookups. The failure is vague on purpose: telling an anonymous
    caller which workspaces exist would make this a customer-list oracle.
    """
    tenant = await session.scalar(
        select(Tenant).where(
            Tenant.slug == workspace.strip().lower(), Tenant.is_active.is_(True)
        )
    )
    if tenant is None:
        raise NotFound("No organisation is registered under that name here.")
    return tenant


async def _throttle(session: AsyncSession, *, tenant_id, email: str) -> None:
    """Two ceilings, both from rows already on the table.

    No stored client IP: see the module docstring. The per-address check is the
    useful one — it stops a loop, and it also catches the common honest case of
    somebody pressing submit twice.
    """
    from datetime import UTC, datetime, timedelta

    since = datetime.now(UTC) - timedelta(hours=1)

    recent = (
        await session.scalar(
            select(func.count())
            .select_from(DsarRequest)
            .where(DsarRequest.submitted_at >= since)
        )
    ) or 0
    if recent >= MAX_PER_TENANT_PER_HOUR:
        raise RateLimited(
            "This organisation has received a large number of requests in the "
            "last hour, so we are pausing new ones briefly. Please try again "
            "shortly — nothing you have submitted has been lost."
        )

    # One unverified request per address at a time. An address with an
    # unconfirmed request already waiting does not get a second: the honest case
    # is a duplicate submit, and the dishonest one is somebody generating work
    # against a mailbox they do not control.
    principal = await session.scalar(
        select(DataPrincipal).where(DataPrincipal.email == email)
    )
    if principal is not None:
        pending = await session.scalar(
            select(func.count())
            .select_from(DsarRequest)
            .where(
                DsarRequest.principal_id == principal.id,
                DsarRequest.verified_at.is_(None),
                DsarRequest.status.in_(("received", "verifying")),
            )
        )
        if pending:
            raise Conflict(
                "There is already a request from this address waiting for "
                "email confirmation. Please use the link we sent you — asking "
                "again does not speed it up, and we cannot act until the "
                "address is confirmed."
            )


@router.post("", response_model=PublicAccepted, status_code=201,
             summary="Raise a rights request without an account")
async def raise_public_request(
    body: PublicRequest,
    unscoped: UnscopedSession,
) -> Any:
    """Accept a request from anyone, and email a confirmation link.

    Two sessions, on purpose. The first resolves the workspace with no tenant
    context — the only way to look a slug up; the second binds that tenant and
    does the write, so RLS applies to every row this creates exactly as it would
    for a signed-in caller. Reusing the unscoped session for the write would
    quietly opt this path out of the isolation everything else depends on.

    NOT dispatched to the engine here. The request exists and its clock runs,
    and nothing executes until the address is confirmed — an unconfirmed erasure
    request must never delete anything.
    """
    if body.type not in ("access", "correction", "completion", "updating",
                         "erasure"):
        raise NotFound(f"Unknown request type {body.type!r}.")

    tenant = await _tenant_by_slug(unscoped, body.workspace)
    tenant_id = tenant.id
    tenant_name = tenant.name

    email = str(body.email).strip().lower()

    async with get_session_factory()() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_id)
            await _throttle(session, tenant_id=tenant_id, email=email)

            # The person may already be on record — a customer whose data the
            # company holds — or entirely unknown to them. Either is normal, and
            # a request from somebody unknown is not less valid: they may be
            # asking precisely because they never signed up.
            principal = await session.scalar(
                select(DataPrincipal).where(DataPrincipal.email == email)
            )
            if principal is None:
                principal = DataPrincipal(
                    tenant_id=tenant_id,
                    # Namespaced so it is obvious where this record came from,
                    # and so it cannot collide with a customer's own ids.
                    external_id=f"public:{email}",
                    email=email,
                    phone=(body.phone or "").strip() or None,
                )
                session.add(principal)
                await session.flush()

            # The actor is the anonymous requester, recorded as such.
            # Attributing this to a system account would make the audit trail
            # say the company raised a request against itself.
            actor = Actor(type="data_principal", id=None, label=email)

            secret, digest = dsar_service.public_token()

            request = await dsar_service.submit(
                session,
                tenant_id=tenant_id,
                actor=actor,
                principal_id=principal.id,
                type=body.type,
                correction_payload=body.details,
                requested_by_actor="principal",
                # Deliberately NOT verified. Confirmation is a separate act,
                # `dispatch_to_engine` is not called here at all, and that
                # function refuses an unconfirmed public request even if a
                # future caller forgets.
                verification_method=None,
                verified=False,
                arrived_publicly=True,
                verification_token_hash=digest,
                public_confirm_url=_confirm_url(
                    workspace=tenant.slug, reference_hint=secret
                ),
            )
            reference = request.reference
            deadline = request.deadline_at

    return PublicAccepted(
        reference=reference,
        deadline_at=deadline,
        confirmation_required=True,
        message=(
            f"Your request has been recorded as {reference}, and {tenant_name} "
            f"must respond by {deadline.date().isoformat()}. Please confirm your "
            "email address using the link we have just sent — the request is "
            "logged and the deadline is running, but nothing will be looked up "
            "or changed until we know the address belongs to you."
        ),
    )


class PublicConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(..., min_length=2, max_length=63)
    #: Both are required. The reference alone is guessable —
    #: DSAR-2026-0007 — and the token alone would be enough if the index were
    #: not unique, so requiring both makes a guessed reference useless without
    #: the emailed secret.
    reference: str = Field(..., min_length=4, max_length=32)
    token: str = Field(..., min_length=16, max_length=128)


class PublicConfirmed(BaseModel):
    reference: str
    confirmed: bool
    message: str


@router.post("/confirm", response_model=PublicConfirmed,
             summary="Confirm the email address on a publicly-raised request")
async def confirm_public_request(
    body: PublicConfirm,
    unscoped: UnscopedSession,
) -> Any:
    """Redeem the emailed token. THIS is what lets the request execute.

    Dispatch happens here rather than at intake, and the ordering is the safety
    property rather than a convenience: an unconfirmed erasure request must
    never delete anything, and `dispatch_to_engine` refuses one even if a future
    caller forgets to check.

    Every failure returns the same message — wrong token, spent token, no such
    reference. A caller must not be able to tell which, because each
    distinction is a fact about somebody else's rights request.
    """
    tenant = await _tenant_by_slug(unscoped, body.workspace)
    tenant_id = tenant.id

    async with get_session_factory()() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_id)

            actor = Actor(type="data_principal", id=None, label="email-confirmed")
            request = await dsar_service.confirm_public(
                session,
                tenant_id=tenant_id,
                actor=actor,
                reference=body.reference,
                token=body.token,
            )
            # Now, and only now.
            await dsar_service.dispatch_to_engine(
                session, tenant_id=tenant_id, actor=actor, request=request
            )
            reference = request.reference

    return PublicConfirmed(
        reference=reference,
        confirmed=True,
        message=(
            f"Thank you — {reference} is confirmed and we are now working on "
            "it. We will contact you at this address with the outcome."
        ),
    )


@router.get("/types", summary="Which rights this organisation's form should offer")
async def rights_offered(
    workspace: str,
    unscoped: UnscopedSession,
) -> dict[str, Any]:
    """What an embedded form renders, and the deadline it should quote.

    Served rather than hardcoded in the snippet so the deadline shown on a
    customer's own website comes from their configured SLA. A form that promises
    30 days while the workspace is set to 15 is worse than one that promises
    nothing.

    The rights themselves are the same for everybody, because the DPDP Act
    applies uniformly — there is no jurisdiction matrix to compute here, which
    is a genuine simplification over the US-state patchwork and not an omission.
    """
    tenant = await _tenant_by_slug(unscoped, workspace)
    return {
        "organisation": tenant.name,
        "workspace": tenant.slug,
        "respond_within_days": tenant.dsar_sla_days,
        "types": [
            {"id": "access", "label": "See my data", "law": "Section 11",
             "needs_details": False},
            {"id": "correction", "label": "Correct something wrong",
             "law": "Section 12(1)", "needs_details": True},
            {"id": "completion", "label": "Add something missing",
             "law": "Section 12(1)", "needs_details": True},
            {"id": "updating", "label": "Update something that has changed",
             "law": "Section 12(1)", "needs_details": True},
            {"id": "erasure", "label": "Erase my data", "law": "Section 12(3)",
             "needs_details": False},
        ],
        # Told to the embedding page so it can set expectations honestly.
        "confirmation_required": True,
    }
