"""Running an assessment: build the questionnaire, assign it, answer it, sign it.

FOUR RULES, each of which is a way an assessment tool stops being evidence.

1. **A published template is frozen.** Editing one produces the next version and
   leaves running assessments pinned to the version they started on. Without
   this, "we assessed this in March" becomes unfalsifiable the moment somebody
   improves a question's wording — the answer stays and the question it answered
   changes underneath it.

2. **Progress accounts for conditional questions.** A required question hidden
   by its `show_if` must not count towards what is outstanding. A tool that
   counts it produces an assessment stuck at 90% that nobody can finish, and the
   universal response to that is to stop using the tool.

3. **Completion is not approval.** Every question answered means the
   questionnaire is full. Somebody still has to read it and put their name to a
   conclusion — and for a DPIA the conclusion *is* the output. An assessment
   with every field filled and no stated finding has documented a process and
   decided nothing.

4. **A signed assessment is not editable.** Answers freeze on approval. The
   whole value of the record is that it says what was known and decided at a
   point in time.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select

from app.core.errors import Conflict, NotFound, ValidationProblem
from app.models.assessment import (
    Assessment,
    AssessmentAnswer,
    AssessmentTemplate,
    TemplateQuestion,
)
from app.models.audit import AuditAction
from app.models.user import User
from app.services import assessment_templates, audit_service, file_service
from app.services.audit_service import Actor

logger = logging.getLogger("app.assessments")


class AssessmentRefused(Conflict):
    """A procedural reason this cannot happen as asked."""


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #

async def seed_built_in(
    session, *, tenant_id: uuid.UUID, created_by: uuid.UUID | None = None
) -> list[AssessmentTemplate]:
    """Install the shipped questionnaires for a new workspace.

    Idempotent by (tenant, slug, version): a workspace that already has version
    1 of the DPIA is left alone, so this can be called on every startup or from
    a backfill without duplicating anything.

    Published immediately, unlike a customer's own drafts. These are ours and
    they are finished; making an admin press Publish on a template they did not
    write would be ceremony.
    """
    installed: list[AssessmentTemplate] = []

    for slug, spec in assessment_templates.BUILT_IN.items():
        exists = await session.scalar(
            select(func.count())
            .select_from(AssessmentTemplate)
            .where(
                AssessmentTemplate.slug == slug,
                AssessmentTemplate.version == 1,
            )
        )
        if exists:
            continue

        now = datetime.now(UTC)
        template = AssessmentTemplate(
            tenant_id=tenant_id,
            slug=slug,
            version=1,
            kind=spec["kind"],
            name=spec["name"],
            description=spec["description"],
            published=True,
            published_at=now,
            built_in=True,
            created_by=created_by,
        )
        session.add(template)
        await session.flush()

        for position, q in enumerate(spec["questions"]):
            session.add(
                _question_row(tenant_id, template.id, position * 10, q)
            )
        await session.flush()
        # `show_if` refers to questions by their stable key; resolve to ids now
        # that every row exists.
        await _resolve_conditions(session, template.id, spec["questions"])
        installed.append(template)

    if installed:
        logger.info(
            "seeded %d built-in assessment template(s) for tenant %s",
            len(installed), tenant_id,
        )
    return installed


def _question_row(
    tenant_id: uuid.UUID, template_id: uuid.UUID, position: int, q: dict[str, Any]
) -> TemplateQuestion:
    return TemplateQuestion(
        tenant_id=tenant_id,
        template_id=template_id,
        position=position,
        section=q.get("section"),
        prompt=q["prompt"],
        helper_text=q.get("helper_text"),
        type=q.get("type", "long_text"),
        required=bool(q.get("required")),
        options=list(q.get("options") or []),
        reportable=bool(q.get("reportable")),
        # Stored under `_key` so the resolver can find its target; replaced with
        # the resolved id below.
        show_if=(
            {"_key": q["show_if"]["key"], "equals": q["show_if"]["equals"]}
            if q.get("show_if")
            else None
        ),
    )


async def _resolve_conditions(
    session, template_id: uuid.UUID, specs: list[dict[str, Any]]
) -> None:
    """Turn `show_if._key` into `show_if.question` (a real question id).

    The templates are authored with stable keys because an author cannot know a
    UUID that does not exist yet. Resolution happens once, at install, so
    evaluating a condition later is a dictionary lookup rather than a join.
    """
    rows = (
        await session.execute(
            select(TemplateQuestion)
            .where(TemplateQuestion.template_id == template_id)
            .order_by(TemplateQuestion.position)
        )
    ).scalars().all()

    # Prompt is unique enough within a template, but the key is what the spec
    # uses — so pair rows to specs positionally, which is how they were created.
    by_key = {spec["key"]: row for spec, row in zip(specs, rows, strict=True)}

    for row in rows:
        if row.show_if and "_key" in row.show_if:
            target = by_key.get(row.show_if["_key"])
            if target is None:
                # A template referring to a question that does not exist would
                # hide the dependent question forever. Drop the condition rather
                # than silently losing the question.
                logger.warning(
                    "template %s has a show_if for unknown key %r; showing the "
                    "question unconditionally",
                    template_id, row.show_if["_key"],
                )
                row.show_if = None
            else:
                row.show_if = {
                    "question": str(target.id),
                    "equals": row.show_if["equals"],
                }


async def get_template(session, *, template_id: uuid.UUID) -> AssessmentTemplate:
    row = await session.scalar(
        select(AssessmentTemplate).where(AssessmentTemplate.id == template_id)
    )
    if row is None:
        raise NotFound("No such template.")
    return row


async def questions_for(session, *, template_id: uuid.UUID) -> list[TemplateQuestion]:
    rows = await session.execute(
        select(TemplateQuestion)
        .where(TemplateQuestion.template_id == template_id)
        .order_by(TemplateQuestion.position)
    )
    return list(rows.scalars().all())


async def list_templates(
    session, *, published_only: bool = False
) -> list[AssessmentTemplate]:
    query = select(AssessmentTemplate)
    if published_only:
        query = query.where(AssessmentTemplate.published)
    rows = await session.execute(
        query.order_by(AssessmentTemplate.kind, AssessmentTemplate.name,
                       AssessmentTemplate.version.desc())
    )
    return list(rows.scalars().all())


async def create_template(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    slug: str,
    kind: str,
    name: str,
    description: str | None = None,
    created_by: uuid.UUID | None = None,
) -> AssessmentTemplate:
    """A new draft questionnaire, at version 1."""
    handle = (slug or "").strip().lower()
    if not handle:
        raise ValidationProblem("A template needs a short identifier.")
    if kind not in (
        "dpia", "ropa", "lia", "tia", "vendor", "discovery", "custom",
    ):
        raise ValidationProblem(f"Unknown template kind {kind!r}.")

    existing = await session.scalar(
        select(func.max(AssessmentTemplate.version)).where(
            AssessmentTemplate.slug == handle
        )
    )
    template = AssessmentTemplate(
        tenant_id=tenant_id,
        slug=handle,
        version=(existing or 0) + 1,
        kind=kind,
        name=(name or handle).strip()[:200],
        description=(description or "").strip() or None,
        published=False,
        created_by=created_by,
    )
    session.add(template)
    await session.flush()
    return template


async def new_version_of(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    template: AssessmentTemplate,
    created_by: uuid.UUID | None = None,
) -> AssessmentTemplate:
    """Copy a published template into an editable next version.

    This is how a published questionnaire is "edited". The alternative — letting
    an admin change a published template in place — silently rewrites the
    question that every existing answer was given to, and there is no way to
    detect afterwards that it happened.
    """
    highest = await session.scalar(
        select(func.max(AssessmentTemplate.version)).where(
            AssessmentTemplate.slug == template.slug
        )
    )
    clone = AssessmentTemplate(
        tenant_id=tenant_id,
        slug=template.slug,
        version=(highest or template.version) + 1,
        kind=template.kind,
        name=template.name,
        description=template.description,
        published=False,
        built_in=False,  # once a customer edits it, it is theirs
        created_by=created_by,
    )
    session.add(clone)
    await session.flush()

    originals = await questions_for(session, template_id=template.id)
    # Map old id -> new id so `show_if` can be rewritten to point inside the
    # clone rather than back at the previous version's questions.
    mapping: dict[str, uuid.UUID] = {}
    for original in originals:
        copy = TemplateQuestion(
            tenant_id=tenant_id,
            template_id=clone.id,
            position=original.position,
            section=original.section,
            prompt=original.prompt,
            helper_text=original.helper_text,
            type=original.type,
            required=original.required,
            options=list(original.options),
            reportable=original.reportable,
            show_if=None,
        )
        session.add(copy)
        await session.flush()
        mapping[str(original.id)] = copy.id

    for original in originals:
        if not original.show_if:
            continue
        target = mapping.get(str(original.show_if.get("question")))
        if target is None:
            continue
        copy_id = mapping[str(original.id)]
        copy = await session.scalar(
            select(TemplateQuestion).where(TemplateQuestion.id == copy_id)
        )
        copy.show_if = {
            "question": str(target),
            "equals": original.show_if.get("equals"),
        }

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.ASSESSMENT_TEMPLATE_VERSIONED,
        entity_type="assessment_template", entity_id=clone.id,
        payload={
            "slug": clone.slug,
            "from_version": template.version,
            "to_version": clone.version,
            "questions": len(originals),
        },
    )
    return clone


async def add_question(
    session,
    *,
    tenant_id: uuid.UUID,
    template: AssessmentTemplate,
    prompt: str,
    type: str = "long_text",
    section: str | None = None,
    helper_text: str | None = None,
    required: bool = False,
    options: list[str] | None = None,
    reportable: bool = False,
    show_if_question: uuid.UUID | None = None,
    show_if_equals: Any = None,
    position: int | None = None,
) -> TemplateQuestion:
    """Add a question. Draft templates only."""
    if template.published:
        raise AssessmentRefused(
            f"{template.name} v{template.version} is published, so its "
            "questions are fixed — they are what people were asked. Create the "
            "next version to change them."
        )
    if type in ("single_choice", "multi_choice") and not options:
        raise ValidationProblem(
            "A choice question needs at least one option, or it renders as a "
            "control nobody can answer."
        )

    highest = await session.scalar(
        select(func.max(TemplateQuestion.position)).where(
            TemplateQuestion.template_id == template.id
        )
    )
    row = TemplateQuestion(
        tenant_id=tenant_id,
        template_id=template.id,
        position=position if position is not None else (highest or 0) + 10,
        section=(section or "").strip() or None,
        prompt=(prompt or "").strip(),
        helper_text=(helper_text or "").strip() or None,
        type=type,
        required=required,
        options=list(options or []),
        reportable=reportable,
        show_if=(
            {"question": str(show_if_question), "equals": show_if_equals}
            if show_if_question is not None
            else None
        ),
    )
    if not row.prompt:
        raise ValidationProblem("A question needs a prompt.")
    session.add(row)
    await session.flush()
    return row


async def delete_question(
    session, *, template: AssessmentTemplate, question_id: uuid.UUID
) -> None:
    if template.published:
        raise AssessmentRefused(
            "Published questions cannot be removed. Create the next version."
        )
    await session.execute(
        delete(TemplateQuestion).where(
            TemplateQuestion.id == question_id,
            TemplateQuestion.template_id == template.id,
        )
    )


async def publish_template(
    session, *, tenant_id: uuid.UUID, actor: Actor, template: AssessmentTemplate
) -> AssessmentTemplate:
    """Freeze it, so it can be used.

    A template with no questions is refused: an assessment against it would be
    instantly "complete" and would attest to nothing.
    """
    if template.published:
        return template

    count = await session.scalar(
        select(func.count()).select_from(TemplateQuestion).where(
            TemplateQuestion.template_id == template.id
        )
    )
    if not count:
        raise AssessmentRefused(
            "A template with no questions would produce assessments that are "
            "complete the moment they are created. Add at least one question."
        )

    template.published = True
    template.published_at = datetime.now(UTC)

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.ASSESSMENT_TEMPLATE_PUBLISHED,
        entity_type="assessment_template", entity_id=template.id,
        payload={
            "slug": template.slug,
            "version": template.version,
            "questions": count,
        },
    )
    return template


# --------------------------------------------------------------------------- #
# Assessments
# --------------------------------------------------------------------------- #

async def create(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    template_id: uuid.UUID,
    name: str,
    description: str | None = None,
    manager_user_id: uuid.UUID | None = None,
    subject_type: str | None = None,
    subject_id: uuid.UUID | None = None,
    subject_label: str | None = None,
    due_at: datetime | None = None,
    review_every_days: int | None = None,
    created_by: uuid.UUID | None = None,
) -> Assessment:
    """Start one. Pins the template version it ran against."""
    template = await get_template(session, template_id=template_id)
    if not template.published:
        raise AssessmentRefused(
            f"{template.name} v{template.version} is still a draft. Publish it "
            "before running an assessment against it — otherwise the questions "
            "can change underneath the answers."
        )

    row = Assessment(
        tenant_id=tenant_id,
        template_id=template.id,
        name=(name or template.name).strip()[:200],
        description=(description or "").strip() or None,
        status="draft",
        manager_user_id=manager_user_id,
        subject_type=subject_type,
        subject_id=subject_id,
        subject_label=(subject_label or "").strip()[:200] or None,
        due_at=due_at,
        review_every_days=review_every_days,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.ASSESSMENT_CREATED,
        entity_type="assessment", entity_id=row.id,
        payload={
            "name": row.name,
            "template": f"{template.slug} v{template.version}",
            "kind": template.kind,
            "subject": row.subject_label,
            "due_at": due_at.isoformat() if due_at else None,
        },
    )
    return row


async def get(session, *, assessment_id: uuid.UUID) -> Assessment:
    row = await session.scalar(
        select(Assessment).where(Assessment.id == assessment_id)
    )
    if row is None:
        raise NotFound("No such assessment.")
    return row


async def answers_for(
    session, *, assessment_id: uuid.UUID
) -> dict[uuid.UUID, AssessmentAnswer]:
    rows = (
        await session.execute(
            select(AssessmentAnswer).where(
                AssessmentAnswer.assessment_id == assessment_id
            )
        )
    ).scalars().all()
    return {r.question_id: r for r in rows}


def _visible(
    question: TemplateQuestion, answers: dict[uuid.UUID, AssessmentAnswer]
) -> bool:
    """Whether this question applies, given what has been answered.

    A question whose condition is not met is not merely hidden in the UI — it is
    excluded from the outstanding count. See rule 2 in the module docstring: a
    required-but-invisible question produces an assessment nobody can complete,
    and the response to that is always to abandon the tool.
    """
    if not question.show_if:
        return True
    target = question.show_if.get("question")
    if not target:
        return True
    try:
        answer = answers.get(uuid.UUID(str(target)))
    except (ValueError, AttributeError):
        return True
    if answer is None:
        return False
    return answer.value == question.show_if.get("equals")


async def progress(
    session, *, assessment: Assessment
) -> dict[str, Any]:
    """How much is done, honestly.

    `outstanding` names the required, visible, unanswered questions. Naming them
    rather than counting them is the difference between "80% complete" and
    something somebody can act on.
    """
    questions = await questions_for(session, template_id=assessment.template_id)
    answers = await answers_for(session, assessment_id=assessment.id)

    visible = [q for q in questions if _visible(q, answers)]
    answered = 0
    outstanding: list[dict[str, Any]] = []

    for question in visible:
        answer = answers.get(question.id)
        done = bool(answer and answer.is_answered)

        if not done and question.type == "evidence" and answer is not None:
            # An evidence question is answered by an upload, so an absent value
            # does not mean an absent answer.
            files = await file_service.for_entity(
                session, entity_type="assessment_answer", entity_id=answer.id,
            )
            done = any(f.is_available for f in files)

        if done:
            answered += 1
        elif question.required:
            outstanding.append({
                "question_id": str(question.id),
                "section": question.section,
                "prompt": question.prompt,
                "assignee_user_id": (
                    str(answer.assignee_user_id)
                    if answer and answer.assignee_user_id else None
                ),
            })

    return {
        "questions": len(visible),
        # Hidden questions are reported so a reviewer can see that the
        # questionnaire narrowed, rather than wondering where they went.
        "hidden": len(questions) - len(visible),
        "answered": answered,
        "percent": round(100 * answered / len(visible)) if visible else 0,
        "outstanding": outstanding,
        "reportable_answered": sum(
            1 for q in visible
            if q.reportable and answers.get(q.id) and answers[q.id].is_answered
        ),
        "reportable_total": sum(1 for q in visible if q.reportable),
        "complete": not outstanding,
    }


async def answer(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    assessment: Assessment,
    question_id: uuid.UUID,
    value: Any = None,
    note: str | None = None,
    answered_by: uuid.UUID | None = None,
) -> AssessmentAnswer:
    """Record or change one answer.

    Refused once approved. The value of the record is that it says what was
    known and decided at a point in time, and an approved assessment whose
    answers can still move says nothing.
    """
    if assessment.status == "approved":
        raise AssessmentRefused(
            f"{assessment.name} was approved on "
            f"{assessment.approved_at:%d %b %Y} and its answers are fixed. "
            "Start a new assessment if the position has changed."
        )
    if assessment.status == "archived":
        raise AssessmentRefused(f"{assessment.name} is archived.")

    question = await session.scalar(
        select(TemplateQuestion).where(
            TemplateQuestion.id == question_id,
            TemplateQuestion.template_id == assessment.template_id,
        )
    )
    if question is None:
        # Includes the case of a question from a DIFFERENT template version,
        # which is exactly what version pinning is meant to prevent.
        raise NotFound("That question is not part of this assessment.")

    _validate_value(question, value)

    row = await session.scalar(
        select(AssessmentAnswer).where(
            AssessmentAnswer.assessment_id == assessment.id,
            AssessmentAnswer.question_id == question_id,
        )
    )
    if row is None:
        row = AssessmentAnswer(
            tenant_id=tenant_id,
            assessment_id=assessment.id,
            question_id=question_id,
        )
        session.add(row)

    row.value = value
    if note is not None:
        row.note = note.strip() or None
    row.answered_by = answered_by
    row.answered_at = datetime.now(UTC)
    await session.flush()

    # First answer moves a draft into progress, so a queue does not show an
    # assessment people are actively filling in as untouched.
    if assessment.status == "draft":
        assessment.status = "in_progress"

    return row


def _validate_value(question: TemplateQuestion, value: Any) -> None:
    """Refuse a value the question cannot hold.

    Checked here rather than trusted from the browser, because these values end
    up in an exported record that somebody relies on — a `boolean` holding the
    string "maybe" is a record that means nothing.
    """
    if value is None:
        return

    kind = question.type
    if kind == "boolean" and not isinstance(value, bool):
        raise ValidationProblem("That question takes yes or no.")
    if kind == "number" and not isinstance(value, int | float):
        raise ValidationProblem("That question takes a number.")
    if kind in ("text", "long_text", "date") and not isinstance(value, str):
        raise ValidationProblem("That question takes text.")
    if kind == "single_choice":
        if value not in question.options:
            raise ValidationProblem(
                f"{value!r} is not one of the choices for that question."
            )
    if kind == "multi_choice":
        if not isinstance(value, list):
            raise ValidationProblem("That question takes a list of choices.")
        unknown = [v for v in value if v not in question.options]
        if unknown:
            raise ValidationProblem(
                f"Not a choice for that question: {', '.join(map(str, unknown))}."
            )


async def assign_question(
    session,
    *,
    tenant_id: uuid.UUID,
    assessment: Assessment,
    question_id: uuid.UUID,
    assignee_user_id: uuid.UUID | None,
) -> AssessmentAnswer:
    """Give one question to somebody.

    Creates the answer row if it does not exist yet, because an assignment has
    to be recorded somewhere and a question with an owner and no row would be
    invisible to everything that reads answers.
    """
    if assignee_user_id is not None:
        target = await session.scalar(
            select(User).where(User.id == assignee_user_id, User.is_active)
        )
        if target is None:
            raise AssessmentRefused(
                "That person does not have an active account in this workspace."
            )

    row = await session.scalar(
        select(AssessmentAnswer).where(
            AssessmentAnswer.assessment_id == assessment.id,
            AssessmentAnswer.question_id == question_id,
        )
    )
    if row is None:
        row = AssessmentAnswer(
            tenant_id=tenant_id,
            assessment_id=assessment.id,
            question_id=question_id,
        )
        session.add(row)
    row.assignee_user_id = assignee_user_id
    await session.flush()
    return row


async def submit(
    session, *, tenant_id: uuid.UUID, actor: Actor, assessment: Assessment
) -> Assessment:
    """Send it for review. Refuses while required questions are outstanding."""
    if assessment.status in ("approved", "archived"):
        raise AssessmentRefused(f"{assessment.name} is {assessment.status}.")

    state = await progress(session, assessment=assessment)
    if not state["complete"]:
        missing = ", ".join(
            o["prompt"][:60] for o in state["outstanding"][:3]
        )
        raise AssessmentRefused(
            f"{len(state['outstanding'])} required question(s) are still "
            f"unanswered: {missing}"
            + ("…" if len(state["outstanding"]) > 3 else "")
        )

    assessment.status = "in_review"
    assessment.submitted_at = datetime.now(UTC)
    assessment.rejection_reason = None

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.ASSESSMENT_SUBMITTED,
        entity_type="assessment", entity_id=assessment.id,
        payload={"name": assessment.name, "questions": state["questions"]},
    )
    return assessment


async def approve(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    assessment: Assessment,
    approver_id: uuid.UUID,
    conclusion: str,
) -> Assessment:
    """Sign it off, with a conclusion.

    The conclusion is required and is the whole output for a DPIA. An assessment
    with every question answered and no stated finding has documented a process
    and decided nothing — which is precisely the criticism made of DPIAs as a
    genre, and the one thing a tool can actually prevent.

    Sets the next review date when a cadence is configured. An assessment is a
    claim about a system at a point in time, and systems change.
    """
    if assessment.status == "approved":
        raise AssessmentRefused(f"{assessment.name} is already approved.")
    if assessment.status == "archived":
        raise AssessmentRefused(f"{assessment.name} is archived.")

    text = (conclusion or "").strip()
    if not text:
        raise ValidationProblem(
            "An approval needs a conclusion. For a DPIA that conclusion is the "
            "output of the exercise — what the residual risk is, and whether "
            "the processing should proceed."
        )

    state = await progress(session, assessment=assessment)
    if not state["complete"]:
        raise AssessmentRefused(
            f"{len(state['outstanding'])} required question(s) are unanswered. "
            "Approving an incomplete assessment would attest to answers nobody "
            "gave."
        )

    now = datetime.now(UTC)
    assessment.status = "approved"
    assessment.approved_by = approver_id
    assessment.approved_at = now
    assessment.conclusion = text
    assessment.rejection_reason = None
    if assessment.review_every_days:
        assessment.next_review_at = now + timedelta(
            days=assessment.review_every_days
        )

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.ASSESSMENT_APPROVED,
        entity_type="assessment", entity_id=assessment.id,
        payload={
            "name": assessment.name,
            "approver": str(approver_id),
            # The conclusion IS the evidence, so it belongs in the chain.
            "conclusion": text[:2000],
            "next_review_at": (
                assessment.next_review_at.isoformat()
                if assessment.next_review_at else None
            ),
        },
    )
    return assessment


async def reject(
    session,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    assessment: Assessment,
    reason: str,
) -> Assessment:
    """Send it back for rework, with a reason.

    Returns to `rejected` rather than to `in_progress`, so a queue can show the
    difference between work that has never been reviewed and work that was
    reviewed and found wanting.
    """
    text = (reason or "").strip()
    if not text:
        raise ValidationProblem(
            "Say what needs to change. A rejection with no reason sends "
            "somebody back to a document with no idea what to fix."
        )
    if assessment.status == "approved":
        raise AssessmentRefused(
            "This is already approved. Start a new assessment rather than "
            "rewriting a signed one."
        )

    assessment.status = "rejected"
    assessment.rejection_reason = text
    assessment.submitted_at = None

    await audit_service.record(
        session, tenant_id=tenant_id, actor=actor,
        action=AuditAction.ASSESSMENT_REJECTED,
        entity_type="assessment", entity_id=assessment.id,
        payload={"name": assessment.name, "reason": text[:1000]},
    )
    return assessment


async def due_for_review(
    session, *, now: datetime | None = None
) -> list[Assessment]:
    """Approved assessments whose review date has passed.

    Read by the scheduler. Returns them rather than acting, because what should
    happen — a reminder, a fresh assessment, an escalation — is a policy
    decision and not this function's to make.
    """
    moment = now or datetime.now(UTC)
    rows = await session.execute(
        select(Assessment).where(
            Assessment.status == "approved",
            Assessment.next_review_at.is_not(None),
            Assessment.next_review_at <= moment,
        )
    )
    return list(rows.scalars().all())


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #

def question_as_dict(q: TemplateQuestion) -> dict[str, Any]:
    return {
        "id": str(q.id),
        "position": q.position,
        "section": q.section,
        "prompt": q.prompt,
        "helper_text": q.helper_text,
        "type": q.type,
        "required": q.required,
        "options": q.options,
        "show_if": q.show_if,
        "reportable": q.reportable,
    }


def template_as_dict(
    t: AssessmentTemplate, questions: list[TemplateQuestion] | None = None
) -> dict[str, Any]:
    out = {
        "id": str(t.id),
        "slug": t.slug,
        "version": t.version,
        "kind": t.kind,
        "name": t.name,
        "description": t.description,
        "published": t.published,
        "published_at": t.published_at,
        "built_in": t.built_in,
    }
    if questions is not None:
        out["questions"] = [question_as_dict(q) for q in questions]
    return out


def answer_as_dict(a: AssessmentAnswer, assignee: User | None = None) -> dict[str, Any]:
    return {
        "question_id": str(a.question_id),
        "value": a.value,
        "note": a.note,
        "assignee_user_id": str(a.assignee_user_id) if a.assignee_user_id else None,
        "assignee_label": (
            (assignee.full_name or assignee.email) if assignee else None
        ),
        "answered_at": a.answered_at,
        # The answer row's id, needed to attach evidence to it.
        "answer_id": str(a.id),
    }


def assessment_as_dict(
    a: Assessment,
    *,
    template: AssessmentTemplate | None = None,
    manager: User | None = None,
) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "name": a.name,
        "description": a.description,
        "status": a.status,
        "template_id": str(a.template_id),
        "template": (
            f"{template.name} v{template.version}" if template else None
        ),
        "kind": template.kind if template else None,
        "manager_user_id": str(a.manager_user_id) if a.manager_user_id else None,
        "manager_label": (
            (manager.full_name or manager.email) if manager else None
        ),
        "subject_type": a.subject_type,
        "subject_id": str(a.subject_id) if a.subject_id else None,
        "subject_label": a.subject_label,
        "due_at": a.due_at,
        # Computed on read against the clock, never stored: an overdue
        # assessment must read as overdue the moment somebody looks, not when a
        # nightly job last ran.
        "overdue": bool(a.due_at and a.is_open and a.due_at < datetime.now(UTC)),
        "review_every_days": a.review_every_days,
        "next_review_at": a.next_review_at,
        "review_due": bool(
            a.next_review_at and a.next_review_at < datetime.now(UTC)
        ),
        "submitted_at": a.submitted_at,
        "approved_at": a.approved_at,
        "conclusion": a.conclusion,
        "rejection_reason": a.rejection_reason,
        "created_at": a.created_at,
    }
