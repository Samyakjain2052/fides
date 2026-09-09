"""Message thread, identity verification, and the stored access package.

The three halves of actually fulfilling a rights request rather than tracking
one. Grouped into a single revision because they are useless apart: a package
with no way to deliver it, or an identity document with nothing recording the
decision made about it, is not a shippable state.

Revision ID: 0016_dsar_fulfilment
Revises: 0015_stored_files
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_dsar_fulfilment"
down_revision: str | None = "0015_stored_files"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"


def upgrade() -> None:
    # ----------------------------------------------------------- messages --
    op.create_table(
        "dsar_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dsar_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("author_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("author_principal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("author_label", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("automated", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_dsar_messages"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_dsar_messages_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["dsar_request_id"], ["dsar_requests.id"],
                                ondelete="CASCADE",
                                name="fk_dsar_messages_dsar_request_id_dsar_requests"),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="SET NULL",
                                name="fk_dsar_messages_author_user_id_users"),
        sa.ForeignKeyConstraint(["author_principal_id"], ["data_principals.id"],
                                ondelete="SET NULL",
                                name="fk_dsar_messages_author_principal_id_data_principals"),
        sa.CheckConstraint("direction IN ('to_principal','from_principal')",
                           name="ck_dsar_messages_direction"),
        sa.CheckConstraint(
            "(author_user_id IS NOT NULL) <> (author_principal_id IS NOT NULL)",
            name="ck_dsar_messages_one_author",
        ),
        sa.CheckConstraint("length(btrim(body)) > 0",
                           name="ck_dsar_messages_body_not_blank"),
    )
    op.create_index("ix_dsar_messages_tenant_id", "dsar_messages", ["tenant_id"])
    op.create_index("ix_dsar_messages_created_at", "dsar_messages", ["created_at"])
    op.create_index(
        "ix_dsar_messages_request_at", "dsar_messages",
        ["dsar_request_id", "created_at"],
    )

    # UPDATE for read/notified receipts. No DELETE: correspondence about a
    # statutory request is evidence of how it was handled, and a thread somebody
    # can tidy up afterwards is not.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON dsar_messages TO {APP_ROLE}")
    op.execute("ALTER TABLE dsar_messages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE dsar_messages FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON dsar_messages
          USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
          WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )

    # ------------------------------------------- request-level new columns --
    #
    # Identity verification as a recorded DECISION, not just a timestamp.
    # `verified_at` already existed and says only that something happened; who
    # decided, on the strength of what, and why a refusal was a refusal are the
    # facts that matter when a rejection is challenged.
    op.add_column(
        "dsar_requests",
        sa.Column("identity_document_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "dsar_requests",
        sa.Column("identity_reviewed_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "dsar_requests",
        sa.Column("identity_reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "dsar_requests",
        sa.Column("identity_rejection_reason", sa.Text(), nullable=True),
    )
    # The assembled package. Points at stored_files; NULL until assembled.
    op.add_column(
        "dsar_requests",
        sa.Column("package_file_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "dsar_requests",
        sa.Column("package_assembled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "dsar_requests",
        sa.Column("package_delivered_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_foreign_key(
        "fk_dsar_requests_identity_document_id_stored_files",
        "dsar_requests", "stored_files",
        ["identity_document_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_dsar_requests_identity_reviewed_by_users",
        "dsar_requests", "users",
        ["identity_reviewed_by"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_dsar_requests_package_file_id_stored_files",
        "dsar_requests", "stored_files",
        ["package_file_id"], ["id"], ondelete="SET NULL",
    )

    # A refusal must carry its reason, the same way a rejected grievance does.
    op.create_check_constraint(
        "ck_dsar_requests_identity_review_recorded",
        "dsar_requests",
        "identity_reviewed_at IS NULL OR identity_reviewed_by IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_dsar_requests_identity_review_recorded", "dsar_requests")
    op.drop_constraint(
        "fk_dsar_requests_package_file_id_stored_files", "dsar_requests"
    )
    op.drop_constraint(
        "fk_dsar_requests_identity_reviewed_by_users", "dsar_requests"
    )
    op.drop_constraint(
        "fk_dsar_requests_identity_document_id_stored_files", "dsar_requests"
    )
    for column in (
        "package_delivered_at", "package_assembled_at", "package_file_id",
        "identity_rejection_reason", "identity_reviewed_at",
        "identity_reviewed_by", "identity_document_id",
    ):
        op.drop_column("dsar_requests", column)

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON dsar_messages")
    op.execute("ALTER TABLE dsar_messages NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE dsar_messages DISABLE ROW LEVEL SECURITY")
    op.drop_table("dsar_messages")
