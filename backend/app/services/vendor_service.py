"""The vendor register, and the facts that follow from it.

WHAT IT REFUSES TO DO

It does not produce a risk score. Competing products publish a number per
vendor derived from litigation feeds, breach databases and diffed privacy
policies; that number is the output of a research operation with people
employed to maintain it. Computing one from what a customer typed into a form
would be inventing an authority we do not have, and the number would be worse
than useless because somebody would rely on it.

WHAT IT DOES INSTEAD

`concerns()` returns individually actionable facts, each with a named cause:

  no signed DPA while in use          §8(2) makes them responsible anyway
  DPA expired                          processing under a lapsed agreement
  no route for a rights request        the obligation is undischargeable
  their SLA exceeds ours               they make us late by default
  no breach undertaking                we cannot notify faster than we are told
  transfers with no location recorded   §16 cannot be assessed
  review overdue                       the assessment is stale
  a document changed since review       somebody should read it again

Each of those is a sentence somebody can act on. A composite score is not, and
the reason products publish one anyway is that a list of specific problems is
harder to sell than a number that goes up.

THE SLA COMPARISON IS THE USEFUL ONE

The statutory clock runs on the fiduciary. A processor who takes 45 days makes
their customer late no matter how good their paperwork is, so `concerns()`
compares the vendor's SLA against the tenant's own and says so.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.errors import Conflict, NotFound, ValidationProblem
from app.models.audit import AuditAction
from app.models.connection import Connection
from app.models.tenant import Tenant
from app.models.user import User
from app.models.vendor import Vendor, VendorDocument, VendorSystem
from app.services import audit_service
from app.services.audit_service import Actor

logger = logging.getLogger("app.vendors")

DOCUMENT_KINDS = (
    "privacy_policy", "dpa", "subprocessor_list", "certification",
    "security_report", "breach_notice", "other",
)


class VendorRefused(Conflict):
    """A procedural reason this cannot happen as asked."""


async def create(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    name: str,
    domain: str | None = None,
    description: str | None = None,
    role: str = "processor",
    risk_tier: str = "medium",
    owner_user_id: uuid.UUID | None = None,
    created_by: uuid.UUID | None = None,
) -> Vendor:
    """Add a vendor, as prospective.

    Starts prospective rather than approved, for the same reason a connection
    starts `unverified`: recording that a vendor exists and deciding they may
    receive personal data are different acts, and a register that conflates them
    approves everything by default.
    """
    label = (name or "").strip()
    if not label:
        raise ValidationProblem("A vendor needs a name.")
    if role not in ("processor", "sub_processor", "joint", "recipient"):
        raise ValidationProblem(f"Unknown role {role!r}.")
    if risk_tier not in ("low", "medium", "high", "critical"):
        raise ValidationProblem(f"Unknown risk tier {risk_tier!r}.")

    row = Vendor(
        tenant_id=tenant_id,
        name=label[:200],
        domain=(domain or "").strip().lower() or None,
        description=(description or "").strip() or None,
        status="prospective",
        role=role,
        risk_tier=risk_tier,
        owner_user_id=owner_user_id,
        created_by=created_by,
    )
    savepoint = await session.begin_nested()
    session.add(row)
    try:
        await savepoint.commit()
    except IntegrityError:
        # A savepoint, so a duplicate name does not poison the caller's whole
        # transaction — the trap that produced a 500 on duplicate retention
        # policy names.
        await savepoint.rollback()
        raise VendorRefused(
            f"{label} is already in the register. Open that record rather than "
            "creating a second one, so their documents and history stay "
            "together."
        ) from None

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.VENDOR_CREATED,
        entity_type="vendor", entity_id=row.id,
        payload={"name": row.name, "role": role, "risk_tier": risk_tier},
    )
    return row


async def get(session, *, vendor_id: uuid.UUID) -> Vendor:
    row = await session.scalar(select(Vendor).where(Vendor.id == vendor_id))
    if row is None:
        raise NotFound("No such vendor.")
    return row


async def list_all(session) -> list[Vendor]:
    rows = await session.execute(select(Vendor).order_by(Vendor.name))
    return list(rows.scalars().all())


async def update(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    vendor: Vendor,
    **fields: Any,
) -> Vendor:
    """Edit the register entry.

    `status` is deliberately NOT settable here — see `decide`. A status change
    is a decision with a reason and an audit entry, and allowing it through a
    generic patch is how a vendor becomes approved with nobody's name on it.
    """
    allowed = {
        "domain", "description", "role", "risk_tier", "owner_user_id",
        "dpa_signed", "dpa_signed_on", "dpa_expires_on",
        "dsar_contact", "dsar_sla_days", "breach_notice_hours",
        "data_categories", "data_location", "transfers_outside_india",
        "subprocessors", "certifications", "review_every_days",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValidationProblem(
            f"Cannot set: {', '.join(sorted(unknown))}."
            + (
                " Use the decision endpoint to change a vendor's status."
                if "status" in unknown else ""
            )
        )

    if fields.get("owner_user_id") is not None:
        owner = await session.scalar(
            select(User).where(
                User.id == fields["owner_user_id"], User.is_active
            )
        )
        if owner is None:
            raise VendorRefused(
                "That person does not have an active account in this workspace."
            )

    # VALIDATE BEFORE MUTATING, and the ordering is not stylistic.
    #
    # Setting the attributes first and refusing afterwards leaves the object
    # dirty in the session, so the caller's eventual commit flushes the change
    # that was just rejected — and what surfaces is an IntegrityError from the
    # database CHECK rather than the sentence written here. Refusing before any
    # attribute moves means a rejected edit changes nothing at all.
    proposed_signed = fields.get("dpa_signed", vendor.dpa_signed)
    proposed_date = fields.get("dpa_signed_on", vendor.dpa_signed_on)
    if proposed_signed and proposed_date is None:
        raise ValidationProblem(
            "A signed agreement needs its date. Without one there is no way to "
            "tell whether it predates the processing it is meant to cover."
        )

    proposed_expiry = fields.get("dpa_expires_on", vendor.dpa_expires_on)
    if proposed_date and proposed_expiry and proposed_expiry < proposed_date:
        raise ValidationProblem(
            "The agreement expires before it was signed. One of those dates is "
            "wrong."
        )

    changed = []
    for key, value in fields.items():
        if getattr(vendor, key) != value:
            setattr(vendor, key, value)
            changed.append(key)

    if changed:
        await audit_service.record(
            session, tenant_id=tenant_id, actor=actor,
            action=AuditAction.VENDOR_UPDATED,
            entity_type="vendor", entity_id=vendor.id,
            payload={"name": vendor.name, "changed": sorted(changed)},
        )
    return vendor


async def decide(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    vendor: Vendor,
    status: str,
    note: str | None = None,
) -> Vendor:
    """Approve, approve with conditions, refuse, or retire.

    A refusal needs a reason and so does a conditional approval — in the second
    case the note IS the record of what remains outstanding, which is the whole
    point of having the state. Most real vendor relationships are conditional
    rather than cleanly approved, and a two-value model forces people to record
    the untrue one.

    Approving sets the review clock if a cadence is configured, so a vendor
    approved today is not still approved on today's evidence in three years.
    """
    if status not in (
        "prospective", "approved", "conditional", "rejected", "retired",
    ):
        raise ValidationProblem(f"Unknown status {status!r}.")

    text = (note or "").strip()
    if status == "rejected" and not text:
        raise ValidationProblem(
            "Refusing a vendor needs a reason. It may be revisited, and "
            "somebody will ask why."
        )
    if status == "conditional" and not text:
        raise ValidationProblem(
            "Say what remains outstanding. A conditional approval whose "
            "conditions are not written down is an unconditional one."
        )

    previous = vendor.status
    vendor.status = status
    if text:
        vendor.decision_note = text

    now = datetime.now(UTC)
    if status in ("approved", "conditional"):
        vendor.last_reviewed_at = now
        if vendor.review_every_days:
            vendor.next_review_at = now + timedelta(days=vendor.review_every_days)

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.VENDOR_DECIDED,
        entity_type="vendor", entity_id=vendor.id,
        payload={
            "name": vendor.name,
            "from": previous,
            "to": status,
            "note": text[:2000] or None,
            "next_review_at": (
                vendor.next_review_at.isoformat()
                if vendor.next_review_at else None
            ),
        },
    )
    return vendor


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #

async def add_document(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    vendor: Vendor,
    kind: str,
    title: str,
    url: str | None = None,
    file_id: uuid.UUID | None = None,
    note: str | None = None,
) -> VendorDocument:
    if kind not in DOCUMENT_KINDS:
        raise ValidationProblem(f"Unknown document kind {kind!r}.")
    if not (url or "").strip() and file_id is None:
        raise ValidationProblem(
            "A document needs either a link or an uploaded file — a row with "
            "neither is a note about nothing."
        )

    row = VendorDocument(
        tenant_id=tenant_id,
        vendor_id=vendor.id,
        kind=kind,
        title=(title or kind.replace("_", " ")).strip()[:200],
        url=(url or "").strip() or None,
        file_id=file_id,
        note=(note or "").strip() or None,
    )
    session.add(row)
    await session.flush()
    return row


async def documents_for(session, *, vendor_id: uuid.UUID) -> list[VendorDocument]:
    rows = await session.execute(
        select(VendorDocument)
        .where(VendorDocument.vendor_id == vendor_id)
        .order_by(VendorDocument.kind, VendorDocument.title)
    )
    return list(rows.scalars().all())


def content_fingerprint(text: str) -> str:
    """Hash of a document's text, normalised.

    Whitespace-collapsed before hashing, so a reflowed paragraph or a changed
    build timestamp does not read as a policy change. The point is to notice a
    substantive edit, and a checker that cries wolf on reformatting gets ignored
    — which is worse than not having one.
    """
    normalised = " ".join((text or "").split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


async def record_review(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    document: VendorDocument,
    content: str | None = None,
) -> VendorDocument:
    """Record that somebody read this document, and what it said.

    Storing the fingerprint is what lets a later look say *whether it changed*.
    Nothing here fetches anything: this holds what was seen and when, which is
    the honest self-hosted version of the policy-diffing commercial products
    sell without pretending to a research operation we do not run.
    """
    document.last_seen_at = datetime.now(UTC)
    document.changed_since_review = False
    if content is not None:
        document.content_hash = content_fingerprint(content)
    return document


async def note_content_changed(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    document: VendorDocument,
    content: str,
) -> bool:
    """Compare fresh content against what was last seen. Returns True if it moved.

    Called by whatever fetched the document — a scheduled job, or a person
    pasting the text in. Kept separate from the fetching so the comparison logic
    has no opinion about how the content arrived.
    """
    fingerprint = content_fingerprint(content)
    if document.content_hash is None:
        # Nothing to compare against; treat this as the first reading rather
        # than as a change, or every new document would arrive "changed".
        document.content_hash = fingerprint
        document.last_seen_at = datetime.now(UTC)
        return False

    if fingerprint == document.content_hash:
        return False

    document.changed_since_review = True
    document.content_hash = fingerprint

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.VENDOR_DOCUMENT_CHANGED,
        entity_type="vendor", entity_id=document.vendor_id,
        payload={"document": document.title, "kind": document.kind},
    )
    return True


# --------------------------------------------------------------------------- #
# Systems
# --------------------------------------------------------------------------- #

async def link_system(
    session,
    *,
    tenant_id: uuid.UUID,
    vendor: Vendor,
    connection_id: uuid.UUID,
) -> VendorSystem:
    """Say that this vendor supplies this connection.

    The join is the useful part: "this vendor is retired — which of our systems
    does that affect?" cannot be answered from two separate lists.
    """
    connection = await session.scalar(
        select(Connection).where(Connection.id == connection_id)
    )
    if connection is None:
        raise NotFound("No such connection.")

    savepoint = await session.begin_nested()
    row = VendorSystem(
        tenant_id=tenant_id, vendor_id=vendor.id, connection_id=connection_id
    )
    session.add(row)
    try:
        await savepoint.commit()
    except IntegrityError:
        await savepoint.rollback()
        existing = await session.scalar(
            select(VendorSystem).where(
                VendorSystem.vendor_id == vendor.id,
                VendorSystem.connection_id == connection_id,
            )
        )
        return existing
    return row


async def systems_for(session, *, vendor_id: uuid.UUID) -> list[Connection]:
    rows = await session.execute(
        select(Connection)
        .join(VendorSystem, VendorSystem.connection_id == Connection.id)
        .where(VendorSystem.vendor_id == vendor_id)
        .order_by(Connection.label)
    )
    return list(rows.scalars().all())


# --------------------------------------------------------------------------- #
# Concerns — what a score would have hidden
# --------------------------------------------------------------------------- #

async def concerns(
    session, *, vendor: Vendor, tenant: Tenant | None = None
) -> list[dict[str, str]]:
    """Individually actionable facts about this vendor.

    Each carries a `severity`, a `title` and a `why` — the last because "no DPA"
    on its own does not tell somebody what turns on it, and a list of flags
    nobody understands gets dismissed as noise.
    """
    out: list[dict[str, str]] = []

    if vendor.dpa_missing:
        out.append({
            "severity": "high",
            "title": "No signed data processing agreement",
            "why": (
                "§8(2) makes you responsible for this processor's processing "
                "whether or not a contract exists. Without one there is nothing "
                "obliging them to act on a rights request you pass on, so an "
                "erasure request covering their systems cannot be discharged."
            ),
        })

    if vendor.dpa_expired:
        out.append({
            "severity": "high",
            "title": f"Agreement expired on {vendor.dpa_expires_on:%d %b %Y}",
            "why": (
                "Every day since has been processing under a lapsed agreement."
            ),
        })
    elif (
        vendor.in_use
        and vendor.dpa_expires_on
        and vendor.dpa_expires_on <= date.today() + timedelta(days=60)
    ):
        out.append({
            "severity": "medium",
            "title": f"Agreement expires {vendor.dpa_expires_on:%d %b %Y}",
            "why": "Renewal takes longer than people expect.",
        })

    if vendor.in_use and not vendor.dsar_contact:
        out.append({
            "severity": "high",
            "title": "No route for a rights request",
            "why": (
                "Nobody has recorded how to ask this vendor to find or delete "
                "one person's data. An access or erasure request covering their "
                "systems has no way to reach them, which makes the obligation "
                "undischargeable rather than merely slow."
            ),
        })

    if tenant is not None and vendor.dsar_sla_days:
        ours = getattr(tenant, "dsar_sla_days", None)
        if ours and vendor.dsar_sla_days >= ours:
            out.append({
                "severity": "high",
                "title": (
                    f"Their {vendor.dsar_sla_days}-day turnaround meets or "
                    f"exceeds your own {ours}-day deadline"
                ),
                "why": (
                    "The statutory clock runs on you, not on them. A processor "
                    "who takes as long as your whole window makes you late by "
                    "default — there is no time left to do anything with what "
                    "they send back."
                ),
            })

    if vendor.in_use and not vendor.breach_notice_hours:
        out.append({
            "severity": "medium",
            "title": "No breach-notification undertaking recorded",
            "why": (
                "§8(6) requires you to notify the Board and the affected people. "
                "You cannot do that faster than this vendor tells you, and "
                "nobody has recorded how fast that is."
            ),
        })

    if vendor.transfers_outside_india and not vendor.data_location:
        out.append({
            "severity": "medium",
            "title": "Transfers outside India with no location recorded",
            "why": (
                "§16 lets the Central Government restrict transfers to notified "
                "countries. Without knowing where the data goes, whether that "
                "restriction applies cannot be assessed."
            ),
        })

    if vendor.review_overdue:
        out.append({
            "severity": "medium",
            "title": (
                f"Review overdue since {vendor.next_review_at:%d %b %Y}"
            ),
            "why": (
                "This vendor is approved on evidence somebody gathered at the "
                "last review. Vendors change hands, change sub-processors and "
                "change regions."
            ),
        })

    if vendor.in_use and not vendor.owner_user_id:
        out.append({
            "severity": "low",
            "title": "No owner",
            "why": (
                "Nobody is answerable for this relationship, so nobody will "
                "notice when any of the above becomes true."
            ),
        })

    documents = await documents_for(session, vendor_id=vendor.id)
    changed = [d for d in documents if d.changed_since_review]
    if changed:
        out.append({
            "severity": "medium",
            "title": (
                f"{len(changed)} document(s) changed since anybody read them"
            ),
            "why": (
                "A privacy policy or sub-processor list that has been edited "
                "since the last review may have changed what this vendor does "
                "with the data: "
                + ", ".join(d.title for d in changed[:3])
            ),
        })

    if vendor.in_use and not vendor.subprocessors and vendor.role == "processor":
        out.append({
            "severity": "low",
            "title": "No sub-processors recorded",
            "why": (
                "Either they use none — worth recording as a fact — or nobody "
                "has asked. §8(2) does not stop at the first hop."
            ),
        })

    return out


def as_dict(
    vendor: Vendor,
    *,
    owner: User | None = None,
    concern_list: list[dict[str, str]] | None = None,
    systems: list[Connection] | None = None,
    documents: list[VendorDocument] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(vendor.id),
        "name": vendor.name,
        "domain": vendor.domain,
        "description": vendor.description,
        "status": vendor.status,
        "risk_tier": vendor.risk_tier,
        "role": vendor.role,
        "decision_note": vendor.decision_note,
        "owner_user_id": (
            str(vendor.owner_user_id) if vendor.owner_user_id else None
        ),
        "owner_label": (owner.full_name or owner.email) if owner else None,
        "dpa_signed": vendor.dpa_signed,
        "dpa_signed_on": vendor.dpa_signed_on,
        "dpa_expires_on": vendor.dpa_expires_on,
        "dpa_expired": vendor.dpa_expired,
        "dpa_missing": vendor.dpa_missing,
        "dsar_contact": vendor.dsar_contact,
        "dsar_sla_days": vendor.dsar_sla_days,
        "breach_notice_hours": vendor.breach_notice_hours,
        "data_categories": vendor.data_categories,
        "data_location": vendor.data_location,
        "transfers_outside_india": vendor.transfers_outside_india,
        "subprocessors": vendor.subprocessors,
        "certifications": vendor.certifications,
        "last_reviewed_at": vendor.last_reviewed_at,
        "review_every_days": vendor.review_every_days,
        "next_review_at": vendor.next_review_at,
        "review_overdue": vendor.review_overdue,
        "in_use": vendor.in_use,
        "created_at": vendor.created_at,
    }
    if concern_list is not None:
        out["concerns"] = concern_list
        # A count, not a score. "Three specific things need attention" is
        # actionable; "68/100" is not, and inventing the second from the first
        # would claim an authority we do not have.
        out["concern_count"] = len(concern_list)
        out["worst_severity"] = (
            "high" if any(c["severity"] == "high" for c in concern_list)
            else "medium" if any(c["severity"] == "medium" for c in concern_list)
            else "low" if concern_list
            else None
        )
    if systems is not None:
        out["systems"] = [
            {"id": str(c.id), "label": c.label, "connector_id": c.connector_id,
             "status": c.status}
            for c in systems
        ]
    if documents is not None:
        out["documents"] = [
            {
                "id": str(d.id),
                "kind": d.kind,
                "title": d.title,
                "url": d.url,
                "file_id": str(d.file_id) if d.file_id else None,
                "last_seen_at": d.last_seen_at,
                "changed_since_review": d.changed_since_review,
                "note": d.note,
            }
            for d in documents
        ]
    return out
