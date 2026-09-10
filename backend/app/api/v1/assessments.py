"""Assessment routes: templates, runs, answers, sign-off.

Three capabilities, and the split is the point — see `Capability`. Reading is
granted to the auditor, because a DPIA is exactly the document an audit exists
to inspect; responding is the widest grant, because the people who know the
answers to a DPIA are not all administrators; approving is the narrowest,
because it is somebody putting their name to a conclusion about risk.

No route writes a `tenant_id` filter: RLS applies it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, require
from app.api.v1.dsar import _read_bounded
from app.core.permissions import Capability
from app.models.assessment import Assessment, AssessmentAnswer
from app.models.user import User
from app.services import assessment_service, file_service

router = APIRouter(prefix="/assessments", tags=["assessments"])


# --------------------------------------------------------------------------- #
# Bodies
# --------------------------------------------------------------------------- #

class TemplateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(..., max_length=64)
    kind: str = Field(..., max_length=16)
    name: str = Field(..., max_length=200)
    description: str | None = Field(None, max_length=4000)


class QuestionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(..., min_length=1)
    type: str = "long_text"
    section: str | None = Field(None, max_length=160)
    helper_text: str | None = None
    required: bool = False
    options: list[str] = Field(default_factory=list)
    reportable: bool = False
    #: Conditional display. The dependent question is excluded from the
    #: outstanding count when its condition is unmet — a required question
    #: nobody can see is how an assessment gets stuck at 90% forever.
    show_if_question: uuid.UUID | None = None
    show_if_equals: Any = None


class AssessmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: uuid.UUID
    name: str = Field(..., max_length=200)
    description: str | None = Field(None, max_length=4000)
    manager_user_id: uuid.UUID | None = None
    subject_type: str | None = Field(None, max_length=32)
    subject_id: uuid.UUID | None = None
    subject_label: str | None = Field(None, max_length=200)
    due_at: datetime | None = None
    #: Recurrence. An assessment is a claim about a system at a point in time.
    review_every_days: int | None = Field(None, gt=0, le=3650)


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Shape depends on the question type; validated server-side against the
    #: question rather than trusted, because these values end up in an exported
    #: record somebody relies on.
    value: Any = None
    note: str | None = Field(None, max_length=4000)


class AssignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignee_user_id: uuid.UUID | None = None


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Required. For a DPIA this is the output of the exercise.
    conclusion: str = Field(..., min_length=1, max_length=8000)


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=4000)


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #

@router.get("/templates", summary="The questionnaires available")
async def list_templates(
    current: Annotated[CurrentUser, Depends(require(Capability.ASSESSMENT_READ))],
    published_only: bool = Query(False),
) -> list[dict[str, Any]]:
    rows = await assessment_service.list_templates(
        current.session, published_only=published_only
    )
    return [assessment_service.template_as_dict(t) for t in rows]


@router.post(
    "/templates", status_code=201, summary="Start a new draft questionnaire"
)
async def create_template(
    body: TemplateIn,
    current: Annotated[CurrentUser, Depends(require(Capability.ASSESSMENT_APPROVE))],
) -> dict[str, Any]:
    template = await assessment_service.create_template(
        current.session,
        tenant_id=current.tenant_id, actor=current.actor,
        slug=body.slug, kind=body.kind, name=body.name,
        description=body.description, created_by=current.user.id,
    )
    return assessment_service.template_as_dict(template, questions=[])


@router.get("/templates/{template_id}", summary="One questionnaire and its questions")
async def get_template(
    template_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.ASSESSMENT_READ))],
) -> dict[str, Any]:
    template = await assessment_service.get_template(
        current.session, template_id=template_id
    )
    questions = await assessment_service.questions_for(
        current.session, template_id=template_id
    )
    return assessment_service.template_as_dict(template, questions=questions)


@router.post(
    "/templates/{template_id}/questions",
    status_code=201,
    summary="Add a question. Draft templates only.",
)
async def add_question(
    template_id: uuid.UUID,
    body: QuestionIn,
    current: Annotated[CurrentUser, Depends(require(Capability.ASSESSMENT_APPROVE))],
) -> dict[str, Any]:
    template = await assessment_service.get_template(
        current.session, template_id=template_id
    )
    question = await assessment_service.add_question(
        current.session,
        tenant_id=current.tenant_id, template=template,
        prompt=body.prompt, type=body.type, section=body.section,
        helper_text=body.helper_text, required=body.required,
        options=body.options, reportable=body.reportable,
        show_if_question=body.show_if_question,
        show_if_equals=body.show_if_equals,
    )
    return assessment_service.question_as_dict(question)


@router.delete(
    "/templates/{template_id}/questions/{question_id}",
    status_code=204,
    summary="Remove a question. Draft templates only.",
)
async def delete_question(
    template_id: uuid.UUID,
    question_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.ASSESSMENT_APPROVE))],
) -> Response:
    # `-> Response`, not `-> None`. FastAPI builds a response model from the
    # return annotation and then asserts a 204 has no body, so `-> None` fails
    # at import time — and because it fails at import, it takes down every route
    # in the app rather than just this one. The same shape `delete_connection`
    # and `logout` already use, for the same reason.
    template = await assessment_service.get_template(
        current.session, template_id=template_id
    )
    await assessment_service.delete_question(
        current.session, template=template, question_id=question_id
    )
    return Response(status_code=204)


@router.post(
    "/templates/{template_id}/publish",
    summary="Freeze a questionnaire so it can be used",
)
async def publish_template(
    template_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.ASSESSMENT_APPROVE))],
) -> dict[str, Any]:
    """Published questions are what people were asked, so they stop being
    editable. Changing one produces the next version."""
    template = await assessment_service.get_template(
        current.session, template_id=template_id
    )
    await assessment_service.publish_template(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        template=template,
    )
    return assessment_service.template_as_dict(template)


@router.post(
    "/templates/{template_id}/new-version",
    status_code=201,
    summary="Copy a published questionnaire into an editable next version",
)
async def new_version(
    template_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.ASSESSMENT_APPROVE))],
) -> dict[str, Any]:
    """How a published questionnaire is edited.

    The alternative — editing in place — silently rewrites the question every
    existing answer was given to, with no way to detect afterwards that it
    happened.
    """
    template = await assessment_service.get_template(
        current.session, template_id=template_id
    )
    clone = await assessment_service.new_version_of(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        template=template, created_by=current.user.id,
    )
    questions = await assessment_service.questions_for(
        current.session, template_id=clone.id
    )
    return assessment_service.template_as_dict(clone, questions=questions)


@router.post(
    "/templates/seed",
    summary="Install the questionnaires this product ships with",
)
async def seed_templates(
    current: Annotated[CurrentUser, Depends(require(Capability.ASSESSMENT_APPROVE))],
) -> dict[str, Any]:
    """Idempotent. A workspace that already has them is left alone.

    Exposed as a route rather than only running at tenant creation, so existing
    workspaces get the templates without a migration that writes tenant data.
    """
    installed = await assessment_service.seed_built_in(
        current.session, tenant_id=current.tenant_id, created_by=current.user.id
    )
    return {
        "installed": [assessment_service.template_as_dict(t) for t in installed],
        "count": len(installed),
    }


# --------------------------------------------------------------------------- #
# Assessments
# --------------------------------------------------------------------------- #

async def _hydrate(current: CurrentUser, row: Assessment) -> dict[str, Any]:
    template = await assessment_service.get_template(
        current.session, template_id=row.template_id
    )
    manager = None
    if row.manager_user_id:
        manager = await current.session.scalar(
            select(User).where(User.id == row.manager_user_id)
        )
    return assessment_service.assessment_as_dict(
        row, template=template, manager=manager
    )


@router.get("", summary="Assessments in this workspace")
async def list_assessments(
    current: Annotated[CurrentUser, Depends(require(Capability.ASSESSMENT_READ))],
    status: str | None = None,
    mine: bool = Query(False, description="Only ones assigned to me"),
) -> list[dict[str, Any]]:
    query = select(Assessment)
    if status:
        query = query.where(Assessment.status == status)
    rows = (
        await current.session.execute(
            query.order_by(Assessment.created_at.desc())
        )
    ).scalars().all()

    if mine:
        # Assigned as the manager, or holding at least one assigned question.
        assigned = set(
            (
                await current.session.execute(
                    select(AssessmentAnswer.assessment_id).where(
                        AssessmentAnswer.assignee_user_id == current.user.id
                    )
                )
            ).scalars().all()
        )
        rows = [
            r for r in rows
            if r.manager_user_id == current.user.id or r.id in assigned
        ]

    return [await _hydrate(current, r) for r in rows]


@router.post("", status_code=201, summary="Start an assessment")
async def create_assessment(
    body: AssessmentIn,
    current: Annotated[CurrentUser, Depends(require(Capability.ASSESSMENT_APPROVE))],
) -> dict[str, Any]:
    """Pins the template version, so later edits to the questionnaire cannot
    change what this assessment asked."""
    row = await assessment_service.create(
        current.session,
        tenant_id=current.tenant_id, actor=current.actor,
        template_id=body.template_id, name=body.name,
        description=body.description,
        manager_user_id=body.manager_user_id or current.user.id,
        subject_type=body.subject_type, subject_id=body.subject_id,
        subject_label=body.subject_label, due_at=body.due_at,
        review_every_days=body.review_every_days,
        created_by=current.user.id,
    )
    return await _hydrate(current, row)


@router.get("/{assessment_id}", summary="One assessment, its questions and answers")
async def get_assessment(
    assessment_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.ASSESSMENT_READ))],
) -> dict[str, Any]:
    row = await assessment_service.get(current.session, assessment_id=assessment_id)
    questions = await assessment_service.questions_for(
        current.session, template_id=row.template_id
    )
    answers = await assessment_service.answers_for(
        current.session, assessment_id=row.id
    )

    ids = {a.assignee_user_id for a in answers.values() if a.assignee_user_id}
    people = {}
    if ids:
        people = {
            u.id: u
            for u in (
                await current.session.execute(select(User).where(User.id.in_(ids)))
            ).scalars().all()
        }

    # Evidence attached per answer, so the screen can render what is already
    # uploaded rather than only an empty file input.
    evidence: dict[str, list[dict[str, Any]]] = {}
    for answer in answers.values():
        files = await file_service.for_entity(
            current.session, entity_type="assessment_answer", entity_id=answer.id
        )
        if files:
            evidence[str(answer.question_id)] = [
                file_service.as_dict(f) for f in files
            ]

    return {
        **await _hydrate(current, row),
        "questions": [assessment_service.question_as_dict(q) for q in questions],
        "answers": {
            str(qid): assessment_service.answer_as_dict(
                a, people.get(a.assignee_user_id)
            )
            for qid, a in answers.items()
        },
        "evidence": evidence,
        "progress": await assessment_service.progress(
            current.session, assessment=row
        ),
    }


@router.put(
    "/{assessment_id}/answers/{question_id}",
    summary="Record or change one answer",
)
async def put_answer(
    assessment_id: uuid.UUID,
    question_id: uuid.UUID,
    body: AnswerIn,
    current: Annotated[CurrentUser, Depends(require(Capability.ASSESSMENT_RESPOND))],
) -> dict[str, Any]:
    """`assessment:respond`, the widest of the three grants.

    A DPIA spans legal, engineering, infrastructure and procurement, and the
    people who know the answers are not all administrators. Requiring
    `assessment:approve` to answer a question is how a DPIA ends up written by
    one person guessing at three other people's work.
    """
    row = await assessment_service.get(current.session, assessment_id=assessment_id)
    answer = await assessment_service.answer(
        current.session,
        tenant_id=current.tenant_id, actor=current.actor, assessment=row,
        question_id=question_id, value=body.value, note=body.note,
        answered_by=current.user.id,
    )
    return {
        "answer": assessment_service.answer_as_dict(answer),
        "progress": await assessment_service.progress(
            current.session, assessment=row
        ),
    }


@router.patch(
    "/{assessment_id}/answers/{question_id}/assign",
    summary="Give one question to somebody",
)
async def assign_question(
    assessment_id: uuid.UUID,
    question_id: uuid.UUID,
    body: AssignIn,
    current: Annotated[CurrentUser, Depends(require(Capability.ASSESSMENT_APPROVE))],
) -> dict[str, Any]:
    row = await assessment_service.get(current.session, assessment_id=assessment_id)
    answer = await assessment_service.assign_question(
        current.session, tenant_id=current.tenant_id, assessment=row,
        question_id=question_id, assignee_user_id=body.assignee_user_id,
    )
    return assessment_service.answer_as_dict(answer)


@router.post(
    "/{assessment_id}/answers/{question_id}/evidence",
    status_code=201,
    summary="Attach a document as the answer to an evidence question",
)
async def upload_evidence(
    assessment_id: uuid.UUID,
    question_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.ASSESSMENT_RESPOND))],
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    """Creates the answer row if needed, so the file has something to hang off.

    Looked up rather than upserted, because `answer()` would overwrite an
    existing assignee and note — and somebody attaching a document to a question
    already assigned to them should not lose the assignment by doing so.
    """
    row = await assessment_service.get(current.session, assessment_id=assessment_id)

    answers = await assessment_service.answers_for(
        current.session, assessment_id=row.id
    )
    answer = answers.get(question_id)
    if answer is None:
        answer = await assessment_service.answer(
            current.session,
            tenant_id=current.tenant_id, actor=current.actor, assessment=row,
            question_id=question_id, value=None,
            answered_by=current.user.id,
        )

    stored = await file_service.store(
        current.session,
        tenant_id=current.tenant_id,
        purpose="assessment_evidence",
        entity_type="assessment_answer",
        entity_id=answer.id,
        filename=file.filename or "evidence",
        data=await _read_bounded(file),
        declared_content_type=file.content_type,
        uploaded_by=current.user.id,
    )
    return file_service.as_dict(stored)


@router.post("/{assessment_id}/submit", summary="Send it for review")
async def submit_assessment(
    assessment_id: uuid.UUID,
    current: Annotated[CurrentUser, Depends(require(Capability.ASSESSMENT_RESPOND))],
) -> dict[str, Any]:
    """Refuses while required, visible questions are unanswered — and names
    them, because "80% complete" is not something anybody can act on."""
    row = await assessment_service.get(current.session, assessment_id=assessment_id)
    await assessment_service.submit(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        assessment=row,
    )
    return await _hydrate(current, row)


@router.post("/{assessment_id}/approve", summary="Sign it off, with a conclusion")
async def approve_assessment(
    assessment_id: uuid.UUID,
    body: ApproveIn,
    current: Annotated[CurrentUser, Depends(require(Capability.ASSESSMENT_APPROVE))],
) -> dict[str, Any]:
    """The conclusion is required.

    For a DPIA it is the entire output of the exercise: an assessment with every
    question answered and no stated finding has documented a process and decided
    nothing.
    """
    row = await assessment_service.get(current.session, assessment_id=assessment_id)
    await assessment_service.approve(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        assessment=row, approver_id=current.user.id, conclusion=body.conclusion,
    )
    return await _hydrate(current, row)


@router.post("/{assessment_id}/reject", summary="Send it back for rework")
async def reject_assessment(
    assessment_id: uuid.UUID,
    body: ReasonIn,
    current: Annotated[CurrentUser, Depends(require(Capability.ASSESSMENT_APPROVE))],
) -> dict[str, Any]:
    row = await assessment_service.get(current.session, assessment_id=assessment_id)
    await assessment_service.reject(
        current.session, tenant_id=current.tenant_id, actor=current.actor,
        assessment=row, reason=body.reason,
    )
    return await _hydrate(current, row)
