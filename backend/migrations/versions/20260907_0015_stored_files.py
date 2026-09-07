"""Uploaded and generated objects — metadata only; bytes live in object storage.

Revision ID: 0015_stored_files
Revises: 0014_password_resets
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_stored_files"
down_revision: str | None = "0014_password_resets"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"


def upgrade() -> None:
    op.create_table(
        "stored_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=True),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("uploaded_by_principal", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("delete_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_stored_files"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_stored_files_tenant_id_tenants"),
        # SET NULL, not CASCADE: an identity document must not disappear from the
        # record because the administrator who reviewed it later left.
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="SET NULL",
                                name="fk_stored_files_uploaded_by_users"),
        sa.ForeignKeyConstraint(["uploaded_by_principal"], ["data_principals.id"],
                                ondelete="SET NULL",
                                name="fk_stored_files_uploaded_by_principal_data_principals"),
        sa.CheckConstraint(
            "purpose IN ('dsar_id_document','dsar_evidence','dsar_package',"
            "'dsar_message','grievance_attachment','assessment_evidence')",
            name="ck_stored_files_purpose",
        ),
        sa.CheckConstraint("byte_size > 0", name="ck_stored_files_not_empty"),
        sa.CheckConstraint(
            "deleted_at IS NULL OR storage_key IS NULL",
            name="ck_stored_files_deleted_has_no_key",
        ),
    )
    op.create_index("ix_stored_files_tenant_id", "stored_files", ["tenant_id"])
    op.create_index("ix_stored_files_created_at", "stored_files", ["created_at"])
    op.create_index(
        "ix_stored_files_tenant_purpose", "stored_files", ["tenant_id", "purpose"]
    )
    op.create_index(
        "ix_stored_files_entity",
        "stored_files",
        ["tenant_id", "entity_type", "entity_id"],
    )
    # Partial: most rows never expire, and the retention sweep only asks for the
    # ones that do.
    op.execute(
        """
        CREATE INDEX ix_stored_files_delete_after ON stored_files (delete_after)
          WHERE delete_after IS NOT NULL AND deleted_at IS NULL
        """
    )

    # UPDATE so a file can be marked purged. No DELETE: destroying the bytes
    # leaves the row, because "we held an ID document and destroyed it on this
    # date" is exactly the fact we may have to prove.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON stored_files TO {APP_ROLE}")

    op.execute("ALTER TABLE stored_files ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE stored_files FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON stored_files
          USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
          WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON stored_files")
    op.execute("ALTER TABLE stored_files NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE stored_files DISABLE ROW LEVEL SECURITY")
    op.drop_table("stored_files")
