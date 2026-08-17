"""Public grievance filing — no credential required.

The only unauthenticated write path in this product, and it is unauthenticated on
purpose. DPDP §13 gives every Data Principal the right to a redressal mechanism,
and a person whose data a company holds may have no account with them at all —
someone whose number was bought from a broker, or who is complaining precisely
*because* they never signed up. A credential you must obtain first is a barrier in
front of a statutory right.

**Why not a publishable key.** The banner API's keys are capped at
`consent:collect` — a ceiling enforced at issue, in the service, and by a CHECK
constraint on the table, because that is what makes "this key cannot do harm"
true rather than aspirational. Widening it to cover grievance filing would trade a
strong, testable property for convenience, and it would only work on pages the
customer had instrumented. So this endpoint stands on its own.

**What replaces the credential**, since something must:

1. **The address must be confirmed** before the complaint can page a Grievance
   Officer. Filing is never blocked on it — the complaint is recorded, counted and
   visible in the queue immediately — but the statutory alarm does not fire on the
   strength of an address nobody has proven they own.
2. **Two throttles**, built from data already on the table rather than from stored
   client IPs. Logging the IP of everyone who files a privacy complaint, in order
   to protect the privacy complaint system, would be a poor trade. One unconfirmed
   complaint per address at a time; a ceiling per workspace per hour.
3. **The workspace is addressed by slug**, which is already public — it is in the
   sign-in URL. There is nothing here to enumerate that a login page does not
   already reveal.

What this endpoint deliberately cannot do: read a grievance, list them, or tell
you anything about one that already exists. It accepts and it confirms. Tracking
requires the account portal, because a status endpoint keyed on a reference
somebody could guess would leak complaints.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UnscopedSession
from app.core.errors import NotFound
from app.db.session import get_session_factory, set_tenant_context
from app.models.grievance import GRIEVANCE_CATEGORIES
from app.models.tenant import Tenant
from app.services import grievance_service
from app.services.audit_service import Actor

router = APIRouter(prefix="/public/v1/grievance", tags=["public API — grievance"])


class PublicFiling(BaseModel):
    workspace: str = Field(
        ..., min_length=2, max_length=63,
        description="The organisation's workspace id — the same one used at "
                    "sign-in.",
    )
    category: str = Field(..., examples=list(GRIEVANCE_CATEGORIES)[:1])
    description: str = Field(
        ..., min_length=10, max_length=grievance_service.MAX_DESCRIPTION
    )
    contact_email: EmailStr = Field(
        ..., description="Required here. Without an account there is no other way "
                         "to answer the complaint, or to confirm it is genuine."
    )


class PublicFiled(BaseModel):
    """Deliberately thin.

    The reference and the deadline, and nothing that could be used to probe for
    other people's complaints. Notably absent: any echo of the description, any
    id, and any indication of whether this address has filed before.
    """

    reference: str
    deadline_at: Any
    confirmation_required: bool
    message: str


class PublicConfirm(BaseModel):
    workspace: str = Field(..., min_length=2, max_length=63)
    reference: str = Field(..., min_length=4, max_length=32)
    token: str = Field(..., min_length=16, max_length=128)


class PublicConfirmed(BaseModel):
    reference: str
    confirmed: bool
    message: str


async def _tenant_by_slug(session: AsyncSession, workspace: str) -> Tenant:
    """Resolve the workspace before tenant context exists.

    `tenants` has no RLS policy, so this is one of the handful of legitimate
    pre-context lookups. The failure is deliberately vague: telling an anonymous
    caller which workspaces exist would turn this into a customer-list oracle.
    """
    tenant = await session.scalar(
        select(Tenant).where(
            Tenant.slug == workspace.strip().lower(), Tenant.is_active.is_(True)
        )
    )
    if tenant is None:
        raise NotFound("No organisation is registered under that name here.")
    return tenant


@router.post("", response_model=PublicFiled, status_code=201,
             summary="File a grievance without an account")
async def file_public(
    body: PublicFiling,
    unscoped: UnscopedSession,
) -> Any:
    """Accept a complaint from anyone, and email a confirmation link.

    Runs in two sessions on purpose. The first resolves the workspace with no
    tenant context (the only way to look a slug up); the second binds that tenant
    and does the write, so RLS applies to every row this creates exactly as it
    would for a signed-in caller. Reusing the unscoped session for the write would
    quietly opt this path out of the isolation everything else depends on.
    """
    tenant = await _tenant_by_slug(unscoped, body.workspace)
    tenant_id = tenant.id
    tenant_name = tenant.name

    async with get_session_factory()() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_id)

            await grievance_service.throttle_anonymous_filing(
                session, tenant_id=tenant_id, contact_email=str(body.contact_email)
            )

            # The actor is the anonymous filer, recorded as such. Attributing this
            # to a system account would make the audit trail say the company filed
            # a complaint against itself.
            actor = Actor(
                type="data_principal", id=None, label=str(body.contact_email)
            )
            grievance, token = await grievance_service.file(
                session,
                tenant_id=tenant_id,
                actor=actor,
                category=body.category,
                description=body.description,
                contact_email=str(body.contact_email),
                require_verification=True,
            )
            reference = grievance.reference
            deadline = grievance.deadline_at

    return PublicFiled(
        reference=reference,
        deadline_at=deadline,
        confirmation_required=True,
        message=(
            f"Your complaint has been recorded as {reference} and {tenant_name} "
            f"must respond by {deadline.date().isoformat()}. Please confirm your "
            "email address using the link we have just sent — until you do, the "
            "complaint is logged but will not trigger escalation to the Grievance "
            "Officer."
        ),
    )


@router.post("/confirm", response_model=PublicConfirmed,
             summary="Confirm the email address on a publicly-filed grievance")
async def confirm_public(
    body: PublicConfirm,
    unscoped: UnscopedSession,
) -> Any:
    """Single-use, time-limited, and constant-time compared.

    The failure message does not distinguish "no such reference" from "wrong
    token" from "expired". One that did would be a way to test whether a given
    reference exists — which is to say, whether a given person complained.
    """
    tenant = await _tenant_by_slug(unscoped, body.workspace)
    tenant_id = tenant.id

    async with get_session_factory()() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_id)
            grievance = await grievance_service.confirm_contact(
                session, tenant_id=tenant_id,
                reference=body.reference.strip().upper(), token=body.token,
            )
            reference = grievance.reference

    return PublicConfirmed(
        reference=reference,
        confirmed=True,
        message=(
            "Thank you — your address is confirmed. Your complaint will now be "
            "escalated to the Grievance Officer if it is not resolved in time."
        ),
    )
