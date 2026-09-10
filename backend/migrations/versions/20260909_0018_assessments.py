"""Assessments: DPIA, RoPA, and the questionnaires behind them.

§10 requires a Data Protection Impact Assessment of every Significant Data
Fiduciary. There was nothing for it, and nothing that could hold a Record of
Processing Activities either.

Revision ID: 0018_assessments
Revises: 0017_action_items
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_assessments"
down_revision: str | None = "0017_action_items"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"

TABLES = (
    "assessment_templates",
    "assessment_template_questions",
    "assessments",
    "assessment_answers",
)


def upgrade() -> None:
    # ------------------------------------------------------- templates --
    op.create_table(
        "assessment_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("published", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("built_in", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_assessment_templates"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_assessment_templates_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL",
                                name="fk_assessment_templates_created_by_users"),
        sa.UniqueConstraint("tenant_id", "slug", "version",
                            name="uq_assessment_templates_slug_version"),
        sa.CheckConstraint(
            "kind IN ('dpia','ropa','lia','tia','vendor','discovery','custom')",
            name="ck_assessment_templates_kind",
        ),
        sa.CheckConstraint("version >= 1",
                           name="ck_assessment_templates_version_positive"),
        sa.CheckConstraint(
            "NOT published OR published_at IS NOT NULL",
            name="ck_assessment_templates_published_ts",
        ),
    )
    op.create_index("ix_assessment_templates_tenant_id", "assessment_templates",
                    ["tenant_id"])
    op.create_index("ix_assessment_templates_created_at", "assessment_templates",
                    ["created_at"])
    op.create_index("ix_assessment_templates_tenant_kind", "assessment_templates",
                    ["tenant_id", "kind"])

    # ------------------------------------------------------- questions --
    op.create_table(
        "assessment_template_questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("section", sa.String(160), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("helper_text", sa.Text(), nullable=True),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("options", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("show_if", postgresql.JSONB(), nullable=True),
        sa.Column("reportable", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_atq"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_atq_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"], ["assessment_templates.id"], ondelete="CASCADE",
            name="fk_atq_template_id_assessment_templates",
        ),
        sa.CheckConstraint(
            "type IN ('text','long_text','single_choice','multi_choice',"
            "'boolean','date','number','evidence')",
            name="ck_atq_type",
        ),
        sa.CheckConstraint(
            "type NOT IN ('single_choice','multi_choice') "
            "OR jsonb_array_length(options) > 0",
            name="ck_atq_choices_have_options",
        ),
    )
    op.create_index("ix_atq_tenant_id",
                    "assessment_template_questions", ["tenant_id"])
    op.create_index("ix_atq_created_at",
                    "assessment_template_questions", ["created_at"])
    op.create_index("ix_template_questions_template",
                    "assessment_template_questions", ["template_id", "position"])

    # ----------------------------------------------------- assessments --
    op.create_table(
        "assessments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="draft"),
        sa.Column("manager_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("subject_type", sa.String(32), nullable=True),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("subject_label", sa.String(200), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_every_days", sa.Integer(), nullable=True),
        sa.Column("next_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("conclusion", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_assessments"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_assessments_tenant_id_tenants"),
        # RESTRICT: a template version that has been used is evidence of what
        # was asked, and deleting it orphans every answer's meaning.
        sa.ForeignKeyConstraint(
            ["template_id"], ["assessment_templates.id"], ondelete="RESTRICT",
            name="fk_assessments_template_id_assessment_templates",
        ),
        sa.ForeignKeyConstraint(["manager_user_id"], ["users.id"],
                                ondelete="SET NULL",
                                name="fk_assessments_manager_user_id_users"),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], ondelete="SET NULL",
                                name="fk_assessments_approved_by_users"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL",
                                name="fk_assessments_created_by_users"),
        sa.CheckConstraint(
            "status IN ('draft','in_progress','in_review','approved',"
            "'rejected','archived')",
            name="ck_assessments_status",
        ),
        sa.CheckConstraint(
            "status <> 'approved' OR "
            "(approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_assessments_approved_has_approver",
        ),
        sa.CheckConstraint(
            "status <> 'rejected' OR rejection_reason IS NOT NULL",
            name="ck_assessments_rejected_has_reason",
        ),
        sa.CheckConstraint(
            "review_every_days IS NULL OR review_every_days > 0",
            name="ck_assessments_cadence_positive",
        ),
    )
    op.create_index("ix_assessments_tenant_id", "assessments", ["tenant_id"])
    op.create_index("ix_assessments_created_at", "assessments", ["created_at"])
    op.create_index("ix_assessments_tenant_status", "assessments",
                    ["tenant_id", "status"])
    op.create_index("ix_assessments_manager", "assessments",
                    ["tenant_id", "manager_user_id"])
    op.create_index("ix_assessments_due", "assessments", ["tenant_id", "due_at"])

    # --------------------------------------------------------- answers --
    op.create_table(
        "assessment_answers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assessment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("question_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignee_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("value", postgresql.JSONB(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("answered_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_assessment_answers"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_assessment_answers_tenant_id_tenants"),
        sa.ForeignKeyConstraint(
            ["assessment_id"], ["assessments.id"], ondelete="CASCADE",
            name="fk_assessment_answers_assessment_id_assessments",
        ),
        sa.ForeignKeyConstraint(
            ["question_id"], ["assessment_template_questions.id"],
            ondelete="CASCADE",
            name="fk_assessment_answers_question_id_atq",
        ),
        sa.ForeignKeyConstraint(["assignee_user_id"], ["users.id"],
                                ondelete="SET NULL",
                                name="fk_assessment_answers_assignee_users"),
        sa.ForeignKeyConstraint(["answered_by"], ["users.id"], ondelete="SET NULL",
                                name="fk_assessment_answers_answered_by_users"),
        sa.UniqueConstraint("assessment_id", "question_id",
                            name="uq_assessment_answers_assess_question"),
    )
    op.create_index("ix_assessment_answers_tenant_id", "assessment_answers",
                    ["tenant_id"])
    op.create_index("ix_assessment_answers_created_at", "assessment_answers",
                    ["created_at"])
    op.create_index("ix_assessment_answers_assessment", "assessment_answers",
                    ["assessment_id"])
    op.create_index("ix_assessment_answers_assignee", "assessment_answers",
                    ["tenant_id", "assignee_user_id"])

    for table in TABLES:
        # DELETE is granted here, unlike on the DSAR tables. A questionnaire in
        # DRAFT is a working document — somebody building a template has to be
        # able to remove a question they just added. Publication is what makes a
        # template immutable, and the service enforces that; a grant is the
        # wrong place to express "only while unpublished".
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
              WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            """
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.drop_table(table)
