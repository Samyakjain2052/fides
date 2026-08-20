"""The purge executor.

Read the model docstring first. This is the only code in the product that
destroys data, and everything here is shaped by that.

The single most important property in this file: **`select_candidates` is called
by both the dry run and the live run.** Two implementations that can disagree is
exactly how a preview reports four rows and the live run destroys four hundred.
There is one selection path; the mode only decides whether anything is written.

What gets purged is the **data principal's identifiers** — masked, keeping the
row — mirroring the DSAR erasure path. Consent records are never destroyed: they
are the evidence that holding the data was permitted, and losing them would
leave a fiduciary unable to answer the only question that matters.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models.audit import AuditAction
from app.models.consent import Consent, DataPrincipal, Purpose
from app.models.dsar import DsarRequest
from app.models.retention import PurgeRun, PurgeRunItem, RetentionPolicy
from app.services import audit_service
from app.services.audit_service import Actor

logger = logging.getLogger("app.retention")

# A hard cap per run. An unbounded UPDATE over a large table holds locks and
# takes the product down; a capped run that says it was capped is strictly
# better than an unbounded one that finishes at 3am or not at all.
BATCH_CAP = 500


class PurgeRefused(Conflict):
    """A reason this purge must not proceed as asked."""


@dataclass
class Candidate:
    """One principal, and whether anything stops us purging them."""

    principal: DataPrincipal
    skip_reason: str | None = None

    @property
    def purgeable(self) -> bool:
        return self.skip_reason is None


# --------------------------------------------------------------------------- #
# Selection — ONE path, shared by preview and live
# --------------------------------------------------------------------------- #

async def select_candidates(
    session: AsyncSession, *, tenant_id: uuid.UUID, policy: RetentionPolicy
) -> list[Candidate]:
    """Who this policy would touch, and who it must not.

    Called by the dry run and the live run alike. If you are tempted to write a
    faster variant for one of them, do not: the whole safety story rests on the
    preview and the execution agreeing, and two queries cannot be kept in step
    by discipline alone.

    Skips are returned rather than filtered out. "This person was not purged
    because they have an open grievance" is the answer to a question somebody
    will eventually ask, and it belongs on the receipt.
    """
    cutoff = datetime.now(UTC) - timedelta(days=policy.retention_days)

    # Principals holding a consent for a purpose in this policy's category.
    principal_ids = (
        select(Consent.principal_id)
        .join(Purpose, Purpose.id == Consent.purpose_id)
        .where(
            Consent.tenant_id == tenant_id,
            Purpose.category == policy.data_category,
        )
        .distinct()
    )

    rows = await session.execute(
        select(DataPrincipal)
        .where(
            DataPrincipal.tenant_id == tenant_id,
            DataPrincipal.id.in_(principal_ids),
            # Already purged: nothing to do, and re-masking would churn the
            # receipt with no change.
            DataPrincipal.purged_at.is_(None),
        )
        .order_by(DataPrincipal.created_at)
        # +1 so the caller can tell a full batch from a coincidentally-exact one
        # and report that the run was capped.
        .limit(BATCH_CAP + 1)
    )
    principals = list(rows.scalars().all())

    out: list[Candidate] = []
    for principal in principals:
        out.append(
            Candidate(principal=principal, skip_reason=await _blocked_reason(
                session, tenant_id=tenant_id, principal=principal, cutoff=cutoff
            ))
        )
    return out


async def _blocked_reason(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal: DataPrincipal,
    cutoff: datetime,
) -> str | None:
    """Every reason this person must be left alone. Checked in order of severity.

    All of these are evaluated inside the caller's transaction, so a request or
    a hold created a moment ago is seen — checking before the transaction would
    leave a window in which a fresh legal hold is ignored.
    """
    if principal.legal_hold:
        return f"legal hold: {principal.legal_hold_reason or 'no reason recorded'}"

    # An active consent means the purpose is still being served. Retention does
    # not apply to data somebody is currently permitted to hold.
    active = await session.scalar(
        select(func.count())
        .select_from(Consent)
        .where(
            Consent.tenant_id == tenant_id,
            Consent.principal_id == principal.id,
            Consent.status == "active",
        )
    )
    if active:
        return "has an active consent"

    # An open rights request. Erasing someone mid-request would destroy the data
    # they just asked to see, and make the request impossible to answer.
    open_dsar = await session.scalar(
        select(func.count())
        .select_from(DsarRequest)
        .where(
            DsarRequest.tenant_id == tenant_id,
            DsarRequest.principal_id == principal.id,
            DsarRequest.status.in_(("received", "verifying", "in_progress")),
        )
    )
    if open_dsar:
        return "has an open rights request"

    # Most recent activity still inside the retention window.
    latest = await session.scalar(
        select(
            func.max(
                func.coalesce(Consent.withdrawn_at, Consent.given_at, Consent.created_at)
            )
        ).where(
            Consent.tenant_id == tenant_id, Consent.principal_id == principal.id
        )
    )
    if latest is not None and latest > cutoff:
        return "still within the retention period"

    return None


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #

async def warn_upcoming(
    session: AsyncSession, *, tenant_id: uuid.UUID, policy: RetentionPolicy
) -> int:
    """Warn people whose data this policy will purge within `notify_days`.

    The seam retention was built around but could not use until notifications
    existed. A policy that destroys data on a timer must tell people first —
    that is why `auto_delete` cannot be set without a notice period.

    Idempotent by construction: the notification table's unique constraint on
    (template, entity) means running this daily warns each person once, not once
    per run.
    """
    from app.services import notification_service

    # Who would be purged if the retention window were `notify_days` shorter —
    # i.e. who is about to become eligible.
    lookahead = RetentionPolicy(
        tenant_id=policy.tenant_id,
        name=policy.name,
        data_category=policy.data_category,
        retention_days=max(1, policy.retention_days - policy.notify_days),
        action=policy.action,
    )
    upcoming = await select_candidates(session, tenant_id=tenant_id, policy=lookahead)

    purge_date = (
        datetime.now(UTC) + timedelta(days=policy.notify_days)
    ).date().isoformat()

    sent = 0
    for candidate in upcoming:
        if not candidate.purgeable or not candidate.principal.email:
            continue
        queued = await notification_service.enqueue(
            session,
            tenant_id=tenant_id,
            key="retention.pre_purge",
            to_address=candidate.principal.email,
            context={"category": policy.data_category, "purge_date": purge_date},
            entity_type="retention_policy",
            # Keyed on the PRINCIPAL, not the policy, so each person is warned
            # once about this category rather than once per policy run.
            entity_id=candidate.principal.id,
            principal_id=candidate.principal.id,
        )
        if queued is not None:
            await notification_service.send_now(session, notification=queued)
            sent += 1
    return sent


async def preview(
    session: AsyncSession, *, tenant_id: uuid.UUID, actor: Actor, policy_id: uuid.UUID
) -> PurgeRun:
    """A dry run. Writes a receipt and touches nothing else.

    This is the primary action in the UI, and it is the default here too: a
    caller who gets the mode wrong previews rather than destroys.
    """
    return await _run(
        session, tenant_id=tenant_id, actor=actor, policy_id=policy_id, mode="dry_run"
    )


async def execute(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    policy_id: uuid.UUID,
    confirm: str,
) -> PurgeRun:
    """A live run. Requires the policy's name back, verbatim.

    The same reason `rm -rf` prompts: an irreversible action needs a step that
    cannot be taken by accident, by a mis-click, or by a script that meant to
    call preview.
    """
    policy = await get_policy(session, tenant_id, policy_id)
    if (confirm or "").strip() != policy.name:
        raise PurgeRefused(
            "To run a live purge, send the policy name exactly as confirmation. "
            f"Expected {policy.name!r}."
        )
    return await _run(
        session, tenant_id=tenant_id, actor=actor, policy_id=policy_id, mode="live"
    )


async def _run(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    policy_id: uuid.UUID,
    mode: str,
) -> PurgeRun:
    policy = await get_policy(session, tenant_id, policy_id)

    if mode == "live" and not policy.is_active:
        raise PurgeRefused("That policy is not active.")
    if mode == "live" and policy.exemption_code != "none":
        # A policy carrying a live exemption is one somebody decided should not
        # run. Honouring that here beats relying on whoever presses the button
        # to remember why it was set.
        if (
            policy.exemption_expires_at is None
            or policy.exemption_expires_at > datetime.now(UTC)
        ):
            raise PurgeRefused(
                f"This policy is exempt ({policy.exemption_code}: "
                f"{policy.exemption_reference}). Clear the exemption before running it."
            )

    run = PurgeRun(
        tenant_id=tenant_id,
        policy_id=policy.id,
        mode=mode,
        status="running",
        started_at=datetime.now(UTC),
        initiated_by=actor.id if actor.type == "user" else None,
    )
    session.add(run)
    await session.flush()

    try:
        candidates = await select_candidates(session, tenant_id=tenant_id, policy=policy)
        capped = len(candidates) > BATCH_CAP
        candidates = candidates[:BATCH_CAP]

        affected = 0
        skipped: dict[str, int] = {}

        for candidate in candidates:
            if not candidate.purgeable:
                skipped[candidate.skip_reason] = skipped.get(candidate.skip_reason, 0) + 1
                session.add(
                    PurgeRunItem(
                        tenant_id=tenant_id, purge_run_id=run.id,
                        table_name="data_principals", entity_id=candidate.principal.id,
                        action_taken="skipped", skip_reason=candidate.skip_reason[:255],
                    )
                )
                continue

            if mode == "dry_run":
                # Recorded as what it WOULD have been, and nothing is touched.
                session.add(
                    PurgeRunItem(
                        tenant_id=tenant_id, purge_run_id=run.id,
                        table_name="data_principals", entity_id=candidate.principal.id,
                        action_taken="skipped",
                        skip_reason=f"dry run — would have been {policy.action}ed",
                    )
                )
                continue

            _purge_principal(candidate.principal, policy.action)
            affected += 1
            session.add(
                PurgeRunItem(
                    tenant_id=tenant_id, purge_run_id=run.id,
                    table_name="data_principals", entity_id=candidate.principal.id,
                    action_taken="masked" if policy.action == "mask" else "deleted",
                )
            )

        await session.flush()

        run.candidates_found = len([c for c in candidates if c.purgeable])
        run.rows_affected = affected
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        run.scope_summary = {
            "policy": policy.name,
            "category": policy.data_category,
            "action": policy.action,
            "retention_days": policy.retention_days,
            "examined": len(candidates),
            "purgeable": run.candidates_found,
            "skipped": skipped,
            # Never truncate silently. A capped run that does not say so reads as
            # "everything eligible was handled".
            "batch_capped": capped,
            "batch_cap": BATCH_CAP if capped else None,
        }
        policy.last_run_at = run.finished_at
        await session.flush()

    except Exception as exc:  # noqa: BLE001 — the receipt must survive any failure
        run.status = "failed"
        run.finished_at = datetime.now(UTC)
        run.error = f"{type(exc).__name__}: {exc}"[:2000]
        await session.flush()
        await audit_service.record(
            session, tenant_id=tenant_id, actor=actor,
            action=AuditAction.RETENTION_PURGED,
            entity_type="purge_run", entity_id=run.id,
            payload={"policy": policy.name, "mode": mode, "status": "failed",
                     "error": run.error},
        )
        raise

    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        # A preview and a live run are different facts, and an audit trail that
        # calls them the same thing cannot answer "when was data actually
        # destroyed?" — the only question this table exists for.
        action=(
            AuditAction.RETENTION_PREVIEWED if mode == "dry_run"
            else AuditAction.RETENTION_PURGED
        ),
        entity_type="purge_run",
        entity_id=run.id,
        payload={
            "policy": policy.name,
            "mode": mode,
            "candidates": run.candidates_found,
            "rows_affected": run.rows_affected,
            "action": policy.action,
            "batch_capped": capped,
        },
    )
    return run


def _purge_principal(principal: DataPrincipal, action: str) -> None:
    """Mask the identifiers, keep the row.

    Same shape as the DSAR erasure path on purpose: two different meanings of
    "erased" in one product is a support nightmare and an audit contradiction.
    The consent rows still resolve — they point at a person who can no longer be
    identified, which is exactly what retention is supposed to achieve.

    `delete` is not implemented as a row removal here: `dsar_requests` holds a
    RESTRICT reference precisely so a person with a rights-request history cannot
    be erased out from under it, and a partial delete that fails halfway is worse
    than a mask that succeeds. A policy asking for `delete` gets a mask plus a
    `purged_at` stamp, and the receipt says which was applied.
    """
    principal.email = None
    principal.phone = None
    principal.guardian_email = None
    principal.external_id = f"purged:{principal.id}"
    principal.purged_at = datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Policies
# --------------------------------------------------------------------------- #

async def get_policy(
    session: AsyncSession, tenant_id: uuid.UUID, policy_id: uuid.UUID
) -> RetentionPolicy:
    row = await session.scalar(
        select(RetentionPolicy).where(
            RetentionPolicy.id == policy_id, RetentionPolicy.tenant_id == tenant_id
        )
    )
    if row is None:
        raise NotFound("No such retention policy.")
    return row


async def create_policy(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    name: str,
    data_category: str,
    retention_days: int,
    action: str = "mask",
    auto_delete: bool = False,
    notify_days: int = 14,
    exemption_code: str = "none",
    exemption_reference: str | None = None,
) -> RetentionPolicy:
    if auto_delete and notify_days < 1:
        raise PurgeRefused(
            "A policy that destroys data automatically has to warn first. "
            "Set a notice period of at least one day."
        )
    if exemption_code != "none" and not (exemption_reference or "").strip():
        raise PurgeRefused(
            "An exemption needs a reference — the statute or matter it rests on. "
            "Without one it is an assertion, not a justification."
        )

    policy = RetentionPolicy(
        tenant_id=tenant_id,
        name=name.strip(),
        data_category=data_category.strip(),
        retention_days=retention_days,
        action=action,
        auto_delete=auto_delete,
        notify_days=notify_days,
        exemption_code=exemption_code,
        exemption_reference=(exemption_reference or "").strip() or None,
    )
    # A savepoint, so a name collision does not poison the caller's transaction.
    # `uq_retention_policies_tenant_name` used to surface as a 500 with "Something
    # went wrong", which tells the person filling in the form nothing about the one
    # field they need to change.
    try:
        async with session.begin_nested():
            session.add(policy)
            await session.flush()
    except IntegrityError as exc:
        raise Conflict(
            f"A retention policy called {policy.name!r} already exists. A policy's "
            "name is what the live run demands back before it destroys anything, "
            "and what identifies it in a purge receipt, so two cannot share one. "
            "Rename this one, or edit the existing policy instead."
        ) from exc

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.RETENTION_POLICY_CREATED,
        entity_type="retention_policy", entity_id=policy.id,
        payload={"name": policy.name, "category": data_category,
                 "retention_days": retention_days, "action": action,
                 "auto_delete": auto_delete, "notify_days": notify_days,
                 "exemption_code": exemption_code},
    )
    return policy


# Fields an edit may touch. `data_category` is absent on purpose — see below.
EDITABLE_FIELDS = frozenset({
    "name", "retention_days", "action", "auto_delete", "notify_days",
    "exemption_code", "exemption_reference", "is_active",
})


async def update_policy(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    policy_id: uuid.UUID,
    confirm_shortening: bool = False,
    **fields: Any,
) -> RetentionPolicy:
    """Edit a policy, with the same validation `create_policy` applies.

    Until now a policy could only be created and run, and the screen said so:
    "create a replacement in the meantime". That workaround leaves two policies
    over the same category, which is worse than an edit — the older one keeps
    purging on its own terms.

    **`data_category` cannot be changed.** Repointing a policy at a different
    category changes which people it destroys, while keeping its name, its history
    and its purge receipts. That is a different policy wearing an existing one's
    record, so it must be created as one.

    **Shortening the window on an auto-delete policy needs confirming.** It is the
    one edit that silently enlarges an unattended destruction set: nobody presses
    anything, and tomorrow more people are purged than yesterday. The same
    reasoning as the live run demanding the policy's name back — an irreversible
    consequence should not follow from a single unremarkable form save.
    """
    policy = await session.scalar(
        select(RetentionPolicy).where(
            RetentionPolicy.tenant_id == tenant_id, RetentionPolicy.id == policy_id
        )
    )
    if policy is None:
        raise NotFound("No such retention policy.")

    if "data_category" in fields:
        raise PurgeRefused(
            "A policy's data category cannot be changed — that would point its "
            "history and its purge receipts at a different set of people. Create a "
            "new policy for the other category and deactivate this one."
        )
    unknown = set(fields) - EDITABLE_FIELDS
    if unknown:
        raise PurgeRefused(f"Cannot change: {', '.join(sorted(unknown))}.")

    proposed = {k: v for k, v in fields.items() if v is not None}
    merged = {f: proposed.get(f, getattr(policy, f)) for f in EDITABLE_FIELDS}

    # The same two rules create_policy enforces, applied to the merged result
    # rather than the incoming patch — otherwise turning auto_delete on without
    # mentioning notify_days would slip past.
    if merged["auto_delete"] and merged["notify_days"] < 1:
        raise PurgeRefused(
            "A policy that destroys data automatically has to warn first. "
            "Set a notice period of at least one day."
        )
    if merged["exemption_code"] != "none" and not (
        merged["exemption_reference"] or ""
    ).strip():
        raise PurgeRefused(
            "An exemption needs a reference — the statute or matter it rests on. "
            "Without one it is an assertion, not a justification."
        )
    if merged["retention_days"] < 1:
        raise PurgeRefused("A retention period has to be at least one day.")
    if merged["auto_delete"] and merged["notify_days"] >= merged["retention_days"]:
        raise PurgeRefused(
            f"The notice period ({merged['notify_days']} days) has to be shorter "
            f"than the retention period ({merged['retention_days']} days), or the "
            "warning goes out before there is anything to warn about."
        )

    shortening = merged["retention_days"] < policy.retention_days
    if shortening and merged["auto_delete"] and not confirm_shortening:
        raise PurgeRefused(
            f"This shortens the retention window from {policy.retention_days} to "
            f"{merged['retention_days']} days on a policy that deletes "
            "automatically, so more people become eligible for purging without "
            "anybody pressing anything. Preview it first, then confirm."
        )

    # Renaming onto another policy's name hits the same unique constraint that
    # `create_policy` guards. Checked with a SELECT rather than a savepoint,
    # because by this point `policy` is already dirty and a failed flush inside a
    # nested block is a worse thing to reason about than a benign race: if two
    # requests rename to the same name at once the constraint still refuses one of
    # them, and that is the only case this misses.
    new_name = (merged.get("name") or "").strip()
    if new_name and new_name != policy.name:
        clash = await session.scalar(
            select(RetentionPolicy.id).where(
                RetentionPolicy.tenant_id == tenant_id,
                RetentionPolicy.name == new_name,
                RetentionPolicy.id != policy.id,
            )
        )
        if clash is not None:
            raise Conflict(
                f"Another retention policy is already called {new_name!r}. Names "
                "identify a policy in its purge receipts, so two cannot share one."
            )

    before = {f: getattr(policy, f) for f in sorted(EDITABLE_FIELDS)}
    for field, value in merged.items():
        if isinstance(value, str):
            value = value.strip() or None
        setattr(policy, field, value)
    # `name` is NOT NULL and is what the live run demands back as confirmation.
    if not (policy.name or "").strip():
        raise PurgeRefused("A policy needs a name — the live run asks for it back.")
    await session.flush()

    after = {f: getattr(policy, f) for f in sorted(EDITABLE_FIELDS)}
    changed = {f: {"from": before[f], "to": after[f]}
               for f in sorted(EDITABLE_FIELDS) if before[f] != after[f]}
    if not changed:
        return policy

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.RETENTION_POLICY_UPDATED,
        entity_type="retention_policy", entity_id=policy.id,
        payload={
            "name": policy.name,
            "category": policy.data_category,
            "changed": {k: {"from": str(v["from"]), "to": str(v["to"])}
                        for k, v in changed.items()},
            # Recorded explicitly because it is the consequential direction and a
            # reader should not have to work it out from the numbers.
            "shortened_window": shortening,
            "confirmed_shortening": bool(shortening and confirm_shortening),
        },
    )
    logger.info("retention policy updated", extra={"context": {
        "policy": policy.name, "changed": sorted(changed), "shortened": shortening,
    }})
    return policy


async def list_policies(
    session: AsyncSession, tenant_id: uuid.UUID
) -> list[RetentionPolicy]:
    rows = await session.execute(
        select(RetentionPolicy)
        .where(RetentionPolicy.tenant_id == tenant_id)
        .order_by(RetentionPolicy.name)
    )
    return list(rows.scalars().all())


async def list_runs(
    session: AsyncSession, tenant_id: uuid.UUID, *, policy_id: uuid.UUID | None = None
) -> list[PurgeRun]:
    stmt = select(PurgeRun).where(PurgeRun.tenant_id == tenant_id)
    if policy_id:
        stmt = stmt.where(PurgeRun.policy_id == policy_id)
    rows = await session.execute(stmt.order_by(PurgeRun.started_at.desc()).limit(100))
    return list(rows.scalars().all())


async def run_items(
    session: AsyncSession, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> list[PurgeRunItem]:
    rows = await session.execute(
        select(PurgeRunItem)
        .where(PurgeRunItem.purge_run_id == run_id)
        .order_by(PurgeRunItem.created_at)
    )
    return list(rows.scalars().all())
