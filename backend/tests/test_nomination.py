"""§14 nomination, and duplicate-request refusal.

The nomination tests concentrate on `invoke`, because that operation hands
somebody other than the data principal the power to obtain or destroy their
entire record. Every guard around it is here, and the most important assertion
in the file is that nothing activates a nomination automatically.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.errors import (
    NotFound,
    PermissionDenied,
    ValidationProblem,
)
from app.db.session import set_tenant_context
from app.models.audit import AuditAction, AuditEvent
from app.models.consent import DataPrincipal
from app.models.dsar import DsarRequest
from app.models.nomination import Nomination
from app.services import dsar_service, nomination_service
from app.services.audit_service import Actor
from app.services.dsar_service import DsarRefused, DuplicateRequest
from app.services.nomination_service import NominationRefused


def _actor(tenant: dict) -> Actor:
    return Actor(type="user", id=tenant["admin_id"], label="dpo@test")


async def _principal(session, tenant, *, email="asha@example.com"):
    row = DataPrincipal(
        tenant_id=tenant["id"],
        external_id=f"c-{uuid.uuid4().hex[:8]}",
        email=email,
    )
    session.add(row)
    await session.flush()
    return row


async def _nominate(session, tenant, principal, **kw):
    return await nomination_service.create(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        principal_id=principal.id,
        nominee_name=kw.pop("nominee_name", "Ravi Patel"),
        nominee_email=kw.pop("nominee_email", "ravi@example.com"),
        **kw,
    )


# --------------------------------------------------------------------------- #
# Making one
# --------------------------------------------------------------------------- #

async def test_a_nomination_starts_pending_and_defaults_to_access_only(
    app_session_factory, tenant_a
):
    """The default is considered: a relative settling an estate usually needs
    records and does not need the power to destroy them."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            nomination, token = await _nominate(session, tenant_a, principal)
    assert nomination.status == "pending"
    assert nomination.scope == "access_only"
    assert nomination.is_live is True
    assert nomination.is_exercisable is False
    assert token


async def test_the_acceptance_token_is_never_stored_in_plaintext(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            nomination, token = await _nominate(session, tenant_a, principal)
    assert nomination.acceptance_token_hash != token
    assert token not in (nomination.acceptance_token_hash or "")


async def test_a_nominee_needs_an_email(app_session_factory, tenant_a):
    """A nominee who has never heard of the arrangement will not act on it."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            with pytest.raises(ValidationProblem) as err:
                await _nominate(
                    session, tenant_a, principal, nominee_email="not-an-address"
                )
    assert "email address" in str(err.value)


async def test_only_one_nomination_may_be_in_force(app_session_factory, tenant_a):
    """§14 says "any other individual", singular.

    Two would leave a fiduciary choosing between two people's instructions at
    the worst possible moment.
    """
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            await _nominate(session, tenant_a, principal)
            with pytest.raises(NominationRefused) as err:
                await _nominate(
                    session, tenant_a, principal, nominee_name="Someone Else",
                    nominee_email="other@example.com",
                )
    assert "already has a nomination" in str(err.value)


async def test_revoking_frees_the_principal_to_nominate_again(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            first, _ = await _nominate(session, tenant_a, principal)
            await nomination_service.revoke(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                nomination=first,
            )
            second, _ = await _nominate(
                session, tenant_a, principal, nominee_name="Priya",
                nominee_email="priya@example.com",
            )
    assert first.status == "revoked"
    assert second.status == "pending"


async def test_revoking_needs_no_reason(app_session_factory, tenant_a):
    """§14 gives a right to nominate, not a right to remain nominated."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            nomination, _ = await _nominate(session, tenant_a, principal)
            await nomination_service.revoke(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                nomination=nomination,
            )
    assert nomination.status == "revoked"
    assert nomination.revoked_at is not None
    assert nomination.revocation_note is None


async def test_the_nominee_is_told_the_nomination_exists(
    app_session_factory, tenant_a
):
    """One of very few messages sent to somebody who is neither the subject nor
    an operator of this product."""
    from app.models.notification import Notification

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            await _nominate(session, tenant_a, principal)

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        rows = (
            await session.execute(
                select(Notification).where(
                    Notification.template_key == "nomination.recorded"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].to_address == "ravi@example.com"
    body = rows[0].pending_body or ""
    assert "section 14" in body
    # And it does NOT imply anything is required of them.
    assert "Nothing is expected of you now" in body


async def test_accepting_moves_it_to_active_and_spends_the_token(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            nomination, token = await _nominate(session, tenant_a, principal)
            nomination_id = nomination.id

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await nomination_service.accept(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                nomination_id=nomination_id, token=token,
            )
    assert row.status == "active"
    assert row.accepted_at is not None
    assert row.acceptance_token_hash is None


async def test_a_wrong_acceptance_token_is_refused_generically(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            nomination, _ = await _nominate(session, tenant_a, principal)
            nomination_id = nomination.id

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            with pytest.raises(NotFound):
                await nomination_service.accept(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    nomination_id=nomination_id, token="wrong",
                )


# --------------------------------------------------------------------------- #
# Invoking — the dangerous one
# --------------------------------------------------------------------------- #

async def test_nothing_activates_a_nomination_automatically(
    app_session_factory, tenant_a
):
    """The most important assertion in this file.

    A nomination is live and dormant. No date, no inactivity heuristic, no
    external feed makes it exercisable — only a human recording evidence. If a
    future change adds automatic activation, this fails.
    """
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            nomination, token = await _nominate(session, tenant_a, principal)
            await nomination_service.accept(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                nomination_id=nomination.id, token=token,
            )
    assert nomination.status == "active"
    assert nomination.is_exercisable is False
    assert nomination.invoked_at is None


async def test_invoking_requires_a_substantive_evidence_note(
    app_session_factory, tenant_a
):
    """"yes" is not a record of what somebody examined."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            nomination, _ = await _nominate(session, tenant_a, principal)
            for note in ("", "yes", "ok fine"):
                with pytest.raises(ValidationProblem) as err:
                    await nomination_service.invoke(
                        session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                        nomination=nomination,
                        staff_user_id=tenant_a["admin_id"],
                        evidence_note=note,
                    )
                assert "what evidence you saw" in str(err.value)


async def test_the_database_refuses_an_invocation_with_no_evidence(
    app_session_factory, tenant_a
):
    """A CHECK, so no future caller can bypass the service.

    The single most consequential constraint in the schema.
    """
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            nomination, _ = await _nominate(session, tenant_a, principal)
            nomination_id = nomination.id

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(
            select(Nomination).where(Nomination.id == nomination_id)
        )
        row.status = "invoked"  # and nothing else
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_invoking_records_the_evidence_in_the_audit_chain(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            nomination, _ = await _nominate(session, tenant_a, principal)
            await nomination_service.invoke(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                nomination=nomination, staff_user_id=tenant_a["admin_id"],
                evidence_note="Saw the original death certificate, "
                              "registration number MH/2026/114233, issued by "
                              "Pune Municipal Corporation.",
            )
    assert nomination.is_exercisable is True

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.NOMINATION_INVOKED
                )
            )
        ).scalars().one()
    assert "MH/2026/114233" in event.payload["evidence"]
    assert event.payload["invoked_by"] == str(tenant_a["admin_id"])


async def test_an_invoked_nomination_cannot_be_revoked_as_the_principals_choice(
    app_session_factory, tenant_a
):
    """The principal is, by the premise, unable to revoke.

    Letting staff revoke as though it were the principal's decision would also
    be a way to shut out a legitimate nominee.
    """
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            nomination, _ = await _nominate(session, tenant_a, principal)
            await nomination_service.invoke(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                nomination=nomination, staff_user_id=tenant_a["admin_id"],
                evidence_note="Death certificate seen, reference ABC/123456.",
            )
            with pytest.raises(NominationRefused) as err:
                await nomination_service.revoke(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    nomination=nomination,
                )
    assert "retract the invocation" in str(err.value)


async def test_retracting_an_invocation_is_recorded_as_a_correction(
    app_session_factory, tenant_a
):
    """Not as the principal's decision — they could not have made it."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            nomination, _ = await _nominate(session, tenant_a, principal)
            await nomination_service.invoke(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                nomination=nomination, staff_user_id=tenant_a["admin_id"],
                evidence_note="Certificate seen, reference ABC/123456.",
            )
            await nomination_service.retract_invocation(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                nomination=nomination,
                reason="Wrong person — the certificate was for a different "
                       "customer with the same name.",
            )
    assert nomination.status == "active"
    assert nomination.invoked_at is None
    assert nomination.evidence_note is None

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        actions = [
            e.action
            for e in (
                await session.execute(select(AuditEvent))
            ).scalars().all()
        ]
    # The invocation survives in the chain: somebody WAS given access.
    assert AuditAction.NOMINATION_INVOKED in actions
    assert AuditAction.NOMINATION_INVOCATION_RETRACTED in actions
    assert AuditAction.NOMINATION_REVOKED not in actions


async def test_retracting_needs_a_reason(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            nomination, _ = await _nominate(session, tenant_a, principal)
            await nomination_service.invoke(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                nomination=nomination, staff_user_id=tenant_a["admin_id"],
                evidence_note="Certificate seen, reference ABC/123456.",
            )
            with pytest.raises(ValidationProblem):
                await nomination_service.retract_invocation(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    nomination=nomination, reason="",
                )


# --------------------------------------------------------------------------- #
# Scope — what the nominee may actually do
# --------------------------------------------------------------------------- #

def test_access_only_permits_access_and_nothing_else():
    n = Nomination(scope="access_only", nominee_email="a@b.c")
    assert n.permits("access") is True
    for other in ("erasure", "correction", "completion", "updating"):
        assert n.permits(other) is False, other


def test_access_and_erasure_permits_exactly_those_two():
    n = Nomination(scope="access_and_erasure", nominee_email="a@b.c")
    assert n.permits("access") is True
    assert n.permits("erasure") is True
    assert n.permits("correction") is False


def test_all_rights_permits_every_known_type():
    n = Nomination(scope="all_rights", nominee_email="a@b.c")
    for t in ("access", "correction", "completion", "updating", "erasure"):
        assert n.permits(t) is True, t


def test_an_unknown_request_type_is_never_permitted():
    """Fails closed: adding a new right without extending `permits` refuses
    rather than quietly granting it."""
    for scope in ("access_only", "access_and_erasure", "all_rights"):
        n = Nomination(scope=scope, nominee_email="a@b.c")
        assert n.permits("some_future_right") is False, scope


async def test_a_nominee_cannot_act_before_the_nomination_is_invoked(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            nomination, _ = await _nominate(session, tenant_a, principal)
            with pytest.raises(PermissionDenied) as err:
                nomination_service.authorise_request(
                    nomination=nomination, request_type="access"
                )
    assert "only act once the nomination has been invoked" in str(err.value)


async def test_a_nominee_cannot_exceed_the_scope_the_principal_chose(
    app_session_factory, tenant_a
):
    """The limit the person themselves set."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            nomination, _ = await _nominate(
                session, tenant_a, principal, scope="access_only"
            )
            await nomination_service.invoke(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                nomination=nomination, staff_user_id=tenant_a["admin_id"],
                evidence_note="Death certificate seen, reference XYZ/99887.",
            )
            # Access is fine.
            nomination_service.authorise_request(
                nomination=nomination, request_type="access"
            )
            with pytest.raises(PermissionDenied) as err:
                nomination_service.authorise_request(
                    nomination=nomination, request_type="erasure"
                )
    assert "chose that limit" in str(err.value)


async def test_a_request_raised_under_a_nomination_records_the_link(
    app_session_factory, tenant_a
):
    """"Who asked for this erasure" has to be answerable, and "the deceased" is
    not the answer."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            nomination, _ = await _nominate(
                session, tenant_a, principal, scope="all_rights"
            )
            await nomination_service.invoke(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                nomination=nomination, staff_user_id=tenant_a["admin_id"],
                evidence_note="Death certificate seen, reference XYZ/99887.",
            )
            request = await dsar_service.submit(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                principal_id=principal.id, type="access",
                nomination_id=nomination.id,
            )
            request_id = request.id

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(
            select(DsarRequest).where(DsarRequest.id == request_id)
        )
    assert row.nomination_id == nomination.id


async def test_nominations_are_isolated_between_tenants(
    app_session_factory, tenant_a, tenant_b
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            nomination, _ = await _nominate(session, tenant_a, principal)
            nomination_id = nomination.id

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_b["id"])
        with pytest.raises(NotFound):
            await nomination_service.get(session, nomination_id=nomination_id)


# --------------------------------------------------------------------------- #
# The §12(1) request types
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "kind,phrase",
    [
        ("correction", "what is wrong and what it should be"),
        ("completion", "what is missing and what should be added"),
        ("updating", "what has changed and what the new value is"),
    ],
)
async def test_each_correction_type_asks_for_what_it_needs(
    app_session_factory, tenant_a, kind, phrase
):
    """Three different operations on a source system, so three different
    prompts. A screen offering only "correction" makes somebody pick the
    nearest wrong word."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            with pytest.raises(DsarRefused) as err:
                await dsar_service.submit(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    principal_id=principal.id, type=kind,
                )
    assert phrase in str(err.value)


@pytest.mark.parametrize(
    "kind", ["access", "correction", "completion", "updating", "erasure"]
)
async def test_every_statutory_right_can_be_raised(
    app_session_factory, tenant_a, kind
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            payload = (
                {"field": "phone", "current": "old", "corrected": "new"}
                if kind in ("correction", "completion", "updating")
                else None
            )
            request = await dsar_service.submit(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                principal_id=principal.id, type=kind,
                correction_payload=payload,
            )
    assert request.type == kind


# --------------------------------------------------------------------------- #
# Duplicate detection
# --------------------------------------------------------------------------- #

async def test_a_second_open_request_of_the_same_kind_is_refused(
    app_session_factory, tenant_a
):
    """Answered by pointing at the one that exists, not by opening a second
    clock against the same work."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            first = await dsar_service.submit(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                principal_id=principal.id, type="access",
            )
            with pytest.raises(DuplicateRequest) as err:
                await dsar_service.submit(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    principal_id=principal.id, type="access",
                )
    message = str(err.value)
    assert first.reference in message
    assert "no need to ask again" in message


async def test_a_different_kind_of_request_is_not_a_duplicate(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            await dsar_service.submit(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                principal_id=principal.id, type="access",
            )
            second = await dsar_service.submit(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                principal_id=principal.id, type="erasure",
            )
    assert second.type == "erasure"


async def test_asking_again_after_the_first_is_closed_is_allowed(
    app_session_factory, tenant_a
):
    """A person may legitimately raise the same request a year later.

    The check is "do they already have one OPEN", not "have they ever asked".
    """
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            first = await dsar_service.submit(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                principal_id=principal.id, type="access",
            )
            # Closed the way the service closes one: `completed` without a
            # `resolved_at` is refused by a CHECK, and rightly so.
            await dsar_service.change_status(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=first, to_status="in_progress",
            )
            await dsar_service.change_status(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=first, to_status="completed",
            )
            principal_id = principal.id

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            second = await dsar_service.submit(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                principal_id=principal_id, type="access",
            )
    assert second.id != first.id


async def test_staff_can_override_the_duplicate_check(
    app_session_factory, tenant_a
):
    """A correction to a different field while the first is still being made is
    a real case."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            principal = await _principal(session, tenant_a)
            payload = {"field": "phone", "current": "a", "corrected": "b"}
            await dsar_service.submit(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                principal_id=principal.id, type="correction",
                correction_payload=payload,
            )
            second = await dsar_service.submit(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                principal_id=principal.id, type="correction",
                correction_payload={"field": "email", "current": "c",
                                    "corrected": "d"},
                allow_duplicate=True,
            )
    assert second.type == "correction"


async def test_two_different_people_are_not_duplicates_of_each_other(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            one = await _principal(session, tenant_a, email="one@example.com")
            two = await _principal(session, tenant_a, email="two@example.com")
            await dsar_service.submit(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                principal_id=one.id, type="access",
            )
            second = await dsar_service.submit(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                principal_id=two.id, type="access",
            )
    assert second.principal_id == two.id
