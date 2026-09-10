"""Assessments: DPIAs, RoPAs, and the questionnaires behind them.

§10 makes a Data Protection Impact Assessment a duty of every Significant Data
Fiduciary, alongside a periodic audit. This product had nothing for either, and
no way to hold a Record of Processing Activities — which is the document a
regulator asks for first and the one nobody can produce from a credential list.

FOUR TABLES, AND WHY IT IS FOUR

  assessment_templates    the questionnaire, versioned
  template_questions      its questions, ordered, typed, optionally conditional
  assessments             one run of a template, with a manager and a due date
  assessment_answers      one answer, with its own assignee and its own history

The split between template and run is the whole design. A DPIA answered in
March and the same DPIA answered in September are different records, and
editing the questionnaire must not retroactively change what somebody was
asked. So a template is VERSIONED and an assessment pins the version it ran
against — otherwise "we assessed this" becomes unfalsifiable the moment anybody
tidies up the question wording.

PER-QUESTION ASSIGNMENT IS NOT A LUXURY

A DPIA asks about lawful basis (legal), retention (engineering), international
transfers (infrastructure) and vendor contracts (procurement). One assignee for
the whole document means one person guessing at three other people's answers,
which is how assessments become fiction. Each answer therefore carries its own
assignee and its own completion state, and the assessment is done when its
answers are.

REVIEW CADENCE

An assessment is a claim about a system at a point in time, and systems change.
`review_every_days` schedules the next one rather than leaving a two-year-old
DPIA sitting there looking current — the same reasoning the retention module
applies to data, applied to the paperwork about it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDMixin

#: What a template is for. Drives nothing functionally except the label and the
#: starter content — the machinery is identical, and pretending a DPIA needs a
#: different engine from a RoPA would double the code for no benefit.
TEMPLATE_KINDS = (
    "dpia",          # §10 — Data Protection Impact Assessment
    "ropa",          # Record of Processing Activities
    "lia",           # Legitimate Interests Assessment
    "tia",           # Transfer Impact Assessment
    "vendor",        # a processor's privacy and security posture
    "discovery",     # "what systems do you use" — populates the data map
    "custom",
)

#: How a question is answered. Deliberately small: every extra type is a new
#: rendering path, a new validation rule and a new way to export, and a
#: questionnaire that cannot be exported is not evidence of anything.
QUESTION_TYPES = (
    "text",
    "long_text",
    "single_choice",
    "multi_choice",
    "boolean",
    "date",
    "number",
    "evidence",   # an uploaded document is the answer
)

ASSESSMENT_STATUSES = (
    "draft",        # created, not yet sent to anybody
    "in_progress",  # assigned and being answered
    "in_review",    # answers complete, awaiting the manager's sign-off
    "approved",
    "rejected",     # sent back for rework
    "archived",
)


class AssessmentTemplate(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A questionnaire, at one version.

    Versioned by row rather than mutated. Editing a published template creates
    the next version and leaves running assessments pinned to the one they
    started on — because a question changed under an answer makes the answer
    mean something nobody said.
    """

    __tablename__ = "assessment_templates"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "slug", "version",
            name="uq_assessment_templates_slug_version",
        ),
        Index("ix_assessment_templates_tenant_kind", "tenant_id", "kind"),
        CheckConstraint(
            "kind IN ('dpia','ropa','lia','tia','vendor','discovery','custom')",
            name="kind",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        # A published template is frozen. Its questions are what somebody was
        # asked, and a published version that can still be edited is a record
        # that can be rewritten after the fact.
        CheckConstraint(
            "NOT published OR published_at IS NOT NULL",
            name="published_has_timestamp",
        ),
    )

    #: Stable across versions — `dpia`, `vendor-privacy`. The pair (slug,
    #: version) identifies a questionnaire.
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    #: True once it can be used. Draft templates are editable; published ones
    #: are not, and a change to a published one produces version+1.
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: True for the templates this product ships. Distinguished so an upgrade
    #: can offer a newer built-in version without touching a customer's own.
    built_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class TemplateQuestion(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One question on one version of a template."""

    __tablename__ = "assessment_template_questions"
    __table_args__ = (
        Index("ix_template_questions_template", "template_id", "position"),
        CheckConstraint(
            "type IN ('text','long_text','single_choice','multi_choice',"
            "'boolean','date','number','evidence')",
            name="type",
        ),
        # Choice questions need choices. A single_choice with an empty options
        # list renders as an unanswerable control, which is worse than a
        # validation error somebody sees while editing.
        CheckConstraint(
            "type NOT IN ('single_choice','multi_choice') "
            "OR jsonb_array_length(options) > 0",
            name="choices_have_options",
        ),
    )

    template_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("assessment_templates.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: Sort order within the template. Sparse integers so a question can be
    #: inserted between two others without renumbering the whole set.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Groups questions under a heading — "Identify the need for a DPIA".
    section: Mapped[str | None] = mapped_column(String(160))

    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    #: Guidance shown under the question. Where a DPIA template earns its keep:
    #: "what types of processing identified as likely high risk are involved?"
    #: is a much better question with a paragraph of help under it.
    helper_text: Mapped[str | None] = mapped_column(Text)

    type: Mapped[str] = mapped_column(String(16), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: `["California", "Colorado", ...]` for choice questions.
    options: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list
    )

    #: Conditional display: `{"question": "<uuid>", "equals": "Yes"}`.
    #:
    #: Evaluated server-side when computing which questions are outstanding, so
    #: a hidden question cannot block completion — a required question nobody
    #: can see is how an assessment gets stuck at 90% forever.
    show_if: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    #: Marks the answer as evidence a regulator would want. Drives the
    #: "reporting" count Osano shows, and more usefully drives what an export
    #: includes.
    reportable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Assessment(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One run of one template version."""

    __tablename__ = "assessments"
    __table_args__ = (
        Index("ix_assessments_tenant_status", "tenant_id", "status"),
        Index("ix_assessments_manager", "tenant_id", "manager_user_id"),
        Index("ix_assessments_due", "tenant_id", "due_at"),
        CheckConstraint(
            "status IN ('draft','in_progress','in_review','approved',"
            "'rejected','archived')",
            name="status",
        ),
        # An approval is somebody putting their name to a document. It needs a
        # name and a date, or it is not an approval.
        CheckConstraint(
            "status <> 'approved' OR "
            "(approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="approved_has_approver",
        ),
        CheckConstraint(
            "status <> 'rejected' OR rejection_reason IS NOT NULL",
            name="rejected_has_reason",
        ),
        CheckConstraint(
            "review_every_days IS NULL OR review_every_days > 0",
            name="cadence_positive",
        ),
    )

    template_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        # RESTRICT: a template version that has been used is evidence of what
        # was asked, and deleting it would orphan every answer's meaning.
        ForeignKey("assessment_templates.id", ondelete="RESTRICT"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")

    #: Who is accountable for the whole thing getting done and signed.
    manager_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    #: What this assessment is ABOUT, when it is about something specific —
    #: a connection (a data store), a vendor. Loose rather than a foreign key
    #: per kind, the same choice `stored_files` makes and for the same reason.
    subject_type: Mapped[str | None] = mapped_column(String(32))
    subject_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    #: Denormalised so an archived assessment still names its subject after the
    #: connection or vendor it concerned has been deleted.
    subject_label: Mapped[str | None] = mapped_column(String(200))

    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Recurrence in days. An assessment is a claim about a system at a point in
    #: time, and systems change; a two-year-old DPIA sitting there looking
    #: current is the failure mode this prevents.
    review_every_days: Mapped[int | None] = mapped_column(Integer)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    #: The manager's conclusion. For a DPIA this is the risk finding, which is
    #: the entire output of the exercise — an assessment with every question
    #: answered and no stated conclusion has documented a process and decided
    #: nothing.
    conclusion: Mapped[str | None] = mapped_column(Text)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def is_open(self) -> bool:
        return self.status in ("draft", "in_progress", "in_review", "rejected")


class AssessmentAnswer(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One answer, with its own assignee.

    A row per (assessment, question), created lazily on first answer or
    assignment. Not pre-created for every question: a template with 80
    questions of which 30 are conditional would otherwise produce 80 rows and a
    progress bar that can never reach 100%.
    """

    __tablename__ = "assessment_answers"
    __table_args__ = (
        UniqueConstraint(
            "assessment_id", "question_id",
            name="uq_assessment_answers_assess_question",
        ),
        Index("ix_assessment_answers_assessment", "assessment_id"),
        Index("ix_assessment_answers_assignee", "tenant_id", "assignee_user_id"),
    )

    assessment_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("assessments.id", ondelete="CASCADE"),
        nullable=False,
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("assessment_template_questions.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: Who has to answer THIS question. See the module docstring: a DPIA spans
    #: legal, engineering, infrastructure and procurement, and one assignee for
    #: the document means one person guessing at three other people's answers.
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    #: Shape depends on the question type: a string, a list for multi_choice, a
    #: bool, an ISO date, a number. JSONB rather than eight typed columns —
    #: seven of which would be NULL on every row.
    value: Mapped[Any | None] = mapped_column(JSONB)

    #: Free-text qualification. An assessment answer of "Yes" is frequently
    #: "Yes, but only for the EU instance", and a product with nowhere to put
    #: the qualifier gets the bare "Yes".
    note: Mapped[str | None] = mapped_column(Text)

    answered_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_answered(self) -> bool:
        """Whether this counts towards completion.

        An evidence question is answered by an upload, so the value may be
        absent while the answer is real — the service checks for attached files
        in that case rather than trusting this alone.
        """
        if self.value is None:
            return False
        if isinstance(self.value, str):
            return bool(self.value.strip())
        if isinstance(self.value, list):
            return len(self.value) > 0
        return True
