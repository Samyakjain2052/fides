"""Assessments: DPIA (§10), RoPA, and the questionnaires behind them.

The tests that matter most are about the four ways an assessment tool stops
being evidence: a template edited under its answers, a progress bar that can
never reach 100%, an approval with no conclusion, and a signed record that can
still be changed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.errors import NotFound, ValidationProblem
from app.db.session import set_tenant_context
from app.models.assessment import (
    Assessment,
    AssessmentTemplate,
    TemplateQuestion,
)
from app.models.audit import AuditAction, AuditEvent
from app.services import assessment_service
from app.services.assessment_service import AssessmentRefused
from app.services.audit_service import Actor


def _actor(tenant: dict) -> Actor:
    return Actor(type="user", id=tenant["admin_id"], label="dpo@test")


async def _template(session, tenant, *, slug="custom-q", published=False):
    """A tiny two-question template, one required."""
    t = await assessment_service.create_template(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        slug=slug, kind="custom", name="A short questionnaire",
    )
    first = await assessment_service.add_question(
        session, tenant_id=tenant["id"], template=t,
        prompt="Does this hold personal data?", type="boolean", required=True,
    )
    second = await assessment_service.add_question(
        session, tenant_id=tenant["id"], template=t,
        prompt="Describe it.", type="long_text", required=True,
        show_if_question=first.id, show_if_equals=True,
    )
    if published:
        await assessment_service.publish_template(
            session, tenant_id=tenant["id"], actor=_actor(tenant), template=t
        )
    return t, first, second


# --------------------------------------------------------------------------- #
# The shipped questionnaires
# --------------------------------------------------------------------------- #

async def test_a_new_workspace_gets_the_built_in_templates(
    app_session_factory, tenant_a
):
    """A workspace whose compliance tooling arrives empty puts authoring a §10
    DPIA on the customer before they have done anything."""
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        rows = await assessment_service.list_templates(session)
    slugs = {r.slug for r in rows}
    assert {"dpia", "ropa", "vendor-privacy", "app-discovery"} <= slugs
    assert all(r.published for r in rows), "shipped templates should be usable"
    assert all(r.built_in for r in rows)


async def test_the_dpia_asks_about_lawful_basis_using_indian_law(
    app_session_factory, tenant_a
):
    """`legitimate interests` is a GDPR concept and not a DPDP basis.

    Offering it would invite a company to record a defence it does not have.
    """
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        dpia = await session.scalar(
            select(AssessmentTemplate).where(AssessmentTemplate.slug == "dpia")
        )
        questions = await assessment_service.questions_for(
            session, template_id=dpia.id
        )

    basis = next(q for q in questions if "basis is this processing lawful" in q.prompt)
    joined = " ".join(basis.options).lower()
    assert "legitimate interests" not in joined
    assert "consent (§6)" in joined
    assert "§7" in joined


async def test_every_dpia_question_carries_guidance(app_session_factory, tenant_a):
    """A template with no help generates confident nonsense.

    Not every question needs a paragraph, but the open-ended ones do — those are
    the ones that produce a one-line answer without prompting.
    """
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        dpia = await session.scalar(
            select(AssessmentTemplate).where(AssessmentTemplate.slug == "dpia")
        )
        questions = await assessment_service.questions_for(
            session, template_id=dpia.id
        )

    unhelped = [
        q.prompt for q in questions
        if q.type == "long_text" and not q.helper_text
    ]
    assert not unhelped, f"long-form questions with no guidance: {unhelped}"


async def test_seeding_twice_installs_nothing_the_second_time(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            again = await assessment_service.seed_built_in(
                session, tenant_id=tenant_a["id"]
            )
    assert again == []


async def test_the_discovery_survey_conditions_are_resolved_to_real_questions(
    app_session_factory, tenant_a
):
    """Templates are authored with stable keys; those resolve to ids at install.

    An unresolved `_key` would leave the condition unevaluatable, and the
    dependent question hidden forever.
    """
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        survey = await session.scalar(
            select(AssessmentTemplate).where(
                AssessmentTemplate.slug == "app-discovery"
            )
        )
        questions = await assessment_service.questions_for(
            session, template_id=survey.id
        )

    ids = {str(q.id) for q in questions}
    conditional = [q for q in questions if q.show_if]
    assert conditional, "the discovery survey should have conditional questions"
    for q in conditional:
        assert "_key" not in q.show_if, "condition was never resolved"
        assert q.show_if["question"] in ids


# --------------------------------------------------------------------------- #
# Template versioning — rule 1
# --------------------------------------------------------------------------- #

async def test_a_published_template_cannot_gain_questions(
    app_session_factory, tenant_a
):
    """Published questions are what people were asked."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            template, _, _ = await _template(session, tenant_a, published=True)
            with pytest.raises(AssessmentRefused) as err:
                await assessment_service.add_question(
                    session, tenant_id=tenant_a["id"], template=template,
                    prompt="One more thing",
                )
    assert "next version" in str(err.value)


async def test_a_new_version_copies_the_questions_and_rewires_conditions(
    app_session_factory, tenant_a
):
    """The copy's `show_if` must point inside the copy, not back at v1.

    Pointing at the previous version's question would make the condition
    unevaluatable against the new assessment's answers — the question would
    never appear.
    """
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            template, first, second = await _template(
                session, tenant_a, published=True
            )
            clone = await assessment_service.new_version_of(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                template=template,
            )
            copies = await assessment_service.questions_for(
                session, template_id=clone.id
            )
            original_ids = {str(first.id), str(second.id)}

    assert clone.version == template.version + 1
    assert clone.published is False
    assert len(copies) == 2
    conditional = next(q for q in copies if q.show_if)
    assert conditional.show_if["question"] not in original_ids
    assert conditional.show_if["question"] in {str(q.id) for q in copies}


async def test_an_empty_template_cannot_be_published(app_session_factory, tenant_a):
    """It would produce assessments that are complete on creation."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            t = await assessment_service.create_template(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                slug="empty", kind="custom", name="Nothing here",
            )
            with pytest.raises(AssessmentRefused) as err:
                await assessment_service.publish_template(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    template=t,
                )
    assert "no questions" in str(err.value)


async def test_a_choice_question_needs_options(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            t = await assessment_service.create_template(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                slug="choices", kind="custom", name="Choices",
            )
            with pytest.raises(ValidationProblem):
                await assessment_service.add_question(
                    session, tenant_id=tenant_a["id"], template=t,
                    prompt="Pick one", type="single_choice", options=[],
                )


async def test_the_database_also_refuses_a_choice_question_with_no_options(
    app_session_factory, tenant_a
):
    """A CHECK, so no future caller can bypass the service."""
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            t = await assessment_service.create_template(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                slug="raw-choices", kind="custom", name="Raw",
            )
            template_id = t.id

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        session.add(
            TemplateQuestion(
                tenant_id=tenant_a["id"], template_id=template_id,
                prompt="Pick", type="multi_choice", options=[],
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_an_unpublished_template_cannot_be_run(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            template, _, _ = await _template(session, tenant_a, published=False)
            with pytest.raises(AssessmentRefused) as err:
                await assessment_service.create(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    template_id=template.id, name="Try it",
                )
    assert "still a draft" in str(err.value)


# --------------------------------------------------------------------------- #
# Progress and conditional questions — rule 2
# --------------------------------------------------------------------------- #

async def _run(factory, tenant, **kw):
    async with factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant["id"])
            template, first, second = await _template(
                session, tenant, published=True
            )
            row = await assessment_service.create(
                session, tenant_id=tenant["id"], actor=_actor(tenant),
                template_id=template.id, name="A run", **kw,
            )
            return row.id, first.id, second.id


async def test_a_hidden_required_question_does_not_block_completion(
    app_session_factory, tenant_a
):
    """The 90%-forever bug.

    A required question hidden by its condition must be excluded from the
    outstanding count. A tool that counts it produces an assessment nobody can
    finish, and the response to that is always to abandon the tool.
    """
    assessment_id, first_id, second_id = await _run(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await assessment_service.get(
                session, assessment_id=assessment_id
            )
            # Answer "no" to the gate, so the follow-up does not apply.
            await assessment_service.answer(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                assessment=row, question_id=first_id, value=False,
            )
            state = await assessment_service.progress(session, assessment=row)

    assert state["complete"] is True, state["outstanding"]
    assert state["questions"] == 1
    assert state["hidden"] == 1
    assert state["percent"] == 100


async def test_a_visible_required_question_does_block_completion(
    app_session_factory, tenant_a
):
    assessment_id, first_id, second_id = await _run(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await assessment_service.get(
                session, assessment_id=assessment_id
            )
            await assessment_service.answer(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                assessment=row, question_id=first_id, value=True,
            )
            state = await assessment_service.progress(session, assessment=row)

    assert state["complete"] is False
    assert state["questions"] == 2
    assert [o["question_id"] for o in state["outstanding"]] == [str(second_id)]


async def test_outstanding_names_the_questions_rather_than_counting_them(
    app_session_factory, tenant_a
):
    """"80% complete" is not something anybody can act on."""
    assessment_id, first_id, _ = await _run(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await assessment_service.get(session, assessment_id=assessment_id)
        state = await assessment_service.progress(session, assessment=row)
    assert state["outstanding"]
    assert all("prompt" in o for o in state["outstanding"])


async def test_the_first_answer_moves_a_draft_into_progress(
    app_session_factory, tenant_a
):
    """So a queue does not show work people are filling in as untouched."""
    assessment_id, first_id, _ = await _run(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await assessment_service.get(
                session, assessment_id=assessment_id
            )
            assert row.status == "draft"
            await assessment_service.answer(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                assessment=row, question_id=first_id, value=True,
            )
            assert row.status == "in_progress"


# --------------------------------------------------------------------------- #
# Answer validation
# --------------------------------------------------------------------------- #

async def test_a_boolean_question_refuses_a_string(app_session_factory, tenant_a):
    """A `boolean` holding "maybe" is a record that means nothing."""
    assessment_id, first_id, _ = await _run(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await assessment_service.get(
                session, assessment_id=assessment_id
            )
            with pytest.raises(ValidationProblem):
                await assessment_service.answer(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    assessment=row, question_id=first_id, value="maybe",
                )


async def test_a_choice_question_refuses_a_value_that_is_not_an_option(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            t = await assessment_service.create_template(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                slug="pickone", kind="custom", name="Pick",
            )
            q = await assessment_service.add_question(
                session, tenant_id=tenant_a["id"], template=t,
                prompt="Risk?", type="single_choice",
                options=["Low", "Medium", "High"], required=True,
            )
            await assessment_service.publish_template(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                template=t,
            )
            row = await assessment_service.create(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                template_id=t.id, name="Run",
            )
            with pytest.raises(ValidationProblem) as err:
                await assessment_service.answer(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    assessment=row, question_id=q.id, value="Catastrophic",
                )
    assert "not one of the choices" in str(err.value)


async def test_a_question_from_another_template_is_refused(
    app_session_factory, tenant_a
):
    """What version pinning is for."""
    assessment_id, _, _ = await _run(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            other, other_q, _ = await _template(
                session, tenant_a, slug="unrelated", published=True
            )
            row = await assessment_service.get(
                session, assessment_id=assessment_id
            )
            with pytest.raises(NotFound):
                await assessment_service.answer(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    assessment=row, question_id=other_q.id, value=True,
                )


# --------------------------------------------------------------------------- #
# Submit, approve, reject — rules 3 and 4
# --------------------------------------------------------------------------- #

async def _completed(factory, tenant, **kw):
    assessment_id, first_id, _ = await _run(factory, tenant, **kw)
    async with factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant["id"])
            row = await assessment_service.get(
                session, assessment_id=assessment_id
            )
            await assessment_service.answer(
                session, tenant_id=tenant["id"], actor=_actor(tenant),
                assessment=row, question_id=first_id, value=False,
            )
    return assessment_id


async def test_submitting_an_incomplete_assessment_is_refused_and_says_which(
    app_session_factory, tenant_a
):
    assessment_id, first_id, _ = await _run(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await assessment_service.get(
                session, assessment_id=assessment_id
            )
            with pytest.raises(AssessmentRefused) as err:
                await assessment_service.submit(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    assessment=row,
                )
    assert "Does this hold personal data?" in str(err.value)


async def test_approval_requires_a_conclusion(app_session_factory, tenant_a):
    """For a DPIA the conclusion IS the output.

    Every question answered and no stated finding has documented a process and
    decided nothing.
    """
    assessment_id = await _completed(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await assessment_service.get(
                session, assessment_id=assessment_id
            )
            with pytest.raises(ValidationProblem) as err:
                await assessment_service.approve(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    assessment=row, approver_id=tenant_a["admin_id"],
                    conclusion="  ",
                )
    assert "needs a conclusion" in str(err.value)


async def test_approving_an_incomplete_assessment_is_refused(
    app_session_factory, tenant_a
):
    """It would attest to answers nobody gave."""
    assessment_id, _, _ = await _run(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await assessment_service.get(
                session, assessment_id=assessment_id
            )
            with pytest.raises(AssessmentRefused) as err:
                await assessment_service.approve(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    assessment=row, approver_id=tenant_a["admin_id"],
                    conclusion="Looks fine",
                )
    assert "unanswered" in str(err.value)


async def test_approval_records_the_conclusion_in_the_audit_chain(
    app_session_factory, tenant_a
):
    assessment_id = await _completed(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await assessment_service.get(
                session, assessment_id=assessment_id
            )
            await assessment_service.submit(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                assessment=row,
            )
            await assessment_service.approve(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                assessment=row, approver_id=tenant_a["admin_id"],
                conclusion="Residual risk is low; proceed with the shorter "
                           "retention period agreed with engineering.",
            )

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.ASSESSMENT_APPROVED
                )
            )
        ).scalars().one()
    assert "Residual risk is low" in event.payload["conclusion"]


async def test_an_approved_assessment_cannot_be_changed(app_session_factory, tenant_a):
    """The value of the record is that it says what was decided when."""
    assessment_id = await _completed(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await assessment_service.get(
                session, assessment_id=assessment_id
            )
            questions = await assessment_service.questions_for(
                session, template_id=row.template_id
            )
            await assessment_service.approve(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                assessment=row, approver_id=tenant_a["admin_id"],
                conclusion="Approved.",
            )
            with pytest.raises(AssessmentRefused) as err:
                await assessment_service.answer(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    assessment=row, question_id=questions[0].id, value=True,
                )
    assert "answers are fixed" in str(err.value)


async def test_the_database_refuses_an_approval_with_no_approver(
    app_session_factory, tenant_a
):
    """An approval needs a name and a date, or it is not an approval."""
    assessment_id = await _completed(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(
            select(Assessment).where(Assessment.id == assessment_id)
        )
        row.status = "approved"  # and nothing else
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_rejecting_requires_a_reason_and_marks_it_rejected_not_in_progress(
    app_session_factory, tenant_a
):
    """A queue must distinguish never-reviewed from reviewed-and-found-wanting."""
    assessment_id = await _completed(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await assessment_service.get(
                session, assessment_id=assessment_id
            )
            await assessment_service.submit(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                assessment=row,
            )
            with pytest.raises(ValidationProblem):
                await assessment_service.reject(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    assessment=row, reason="",
                )
            await assessment_service.reject(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                assessment=row,
                reason="The retention answer does not say what performs the "
                       "deletion.",
            )
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(
            select(Assessment).where(Assessment.id == assessment_id)
        )
    assert row.status == "rejected"
    assert row.submitted_at is None
    assert "does not say what performs" in row.rejection_reason


async def test_an_approved_assessment_cannot_be_rejected(
    app_session_factory, tenant_a
):
    assessment_id = await _completed(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await assessment_service.get(
                session, assessment_id=assessment_id
            )
            await assessment_service.approve(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                assessment=row, approver_id=tenant_a["admin_id"],
                conclusion="Done.",
            )
            with pytest.raises(AssessmentRefused) as err:
                await assessment_service.reject(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    assessment=row, reason="changed my mind",
                )
    assert "Start a new assessment" in str(err.value)


# --------------------------------------------------------------------------- #
# Review cadence
# --------------------------------------------------------------------------- #

async def test_approving_with_a_cadence_schedules_the_next_review(
    app_session_factory, tenant_a
):
    """A two-year-old DPIA sitting there looking current is the failure mode."""
    assessment_id = await _completed(
        app_session_factory, tenant_a, review_every_days=365
    )
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await assessment_service.get(
                session, assessment_id=assessment_id
            )
            await assessment_service.approve(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                assessment=row, approver_id=tenant_a["admin_id"],
                conclusion="Fine for now.",
            )
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(
            select(Assessment).where(Assessment.id == assessment_id)
        )
    assert row.next_review_at is not None
    assert row.next_review_at > datetime.now(UTC) + timedelta(days=360)


async def test_an_overdue_review_is_reported(app_session_factory, tenant_a):
    assessment_id = await _completed(
        app_session_factory, tenant_a, review_every_days=1
    )
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await assessment_service.get(
                session, assessment_id=assessment_id
            )
            await assessment_service.approve(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                assessment=row, approver_id=tenant_a["admin_id"],
                conclusion="Fine.",
            )
            row.next_review_at = datetime.now(UTC) - timedelta(days=1)

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        due = await assessment_service.due_for_review(session)
    assert [a.id for a in due] == [assessment_id]


async def test_an_overdue_due_date_is_computed_on_read(
    app_session_factory, tenant_a
):
    """Never stored: an overdue assessment must read as overdue the moment
    somebody looks, not when a nightly job last ran."""
    assessment_id, _, _ = await _run(
        app_session_factory, tenant_a,
        due_at=datetime.now(UTC) - timedelta(days=2),
    )
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await assessment_service.get(session, assessment_id=assessment_id)
        out = assessment_service.assessment_as_dict(row)
    assert out["overdue"] is True


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #

async def test_assessments_are_isolated_between_tenants(
    app_session_factory, tenant_a, tenant_b
):
    assessment_id, _, _ = await _run(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_b["id"])
        with pytest.raises(NotFound):
            await assessment_service.get(session, assessment_id=assessment_id)


async def test_each_workspace_gets_its_own_copy_of_the_templates(
    app_session_factory, tenant_a, tenant_b
):
    """Templates are tenant-scoped, so a customer can edit theirs without
    touching anybody else's."""
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        a = await session.scalar(
            select(func.count()).select_from(AssessmentTemplate)
        )
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_b["id"])
        b = await session.scalar(
            select(func.count()).select_from(AssessmentTemplate)
        )
    assert a == b == len(
        __import__(
            "app.services.assessment_templates", fromlist=["BUILT_IN"]
        ).BUILT_IN
    )
