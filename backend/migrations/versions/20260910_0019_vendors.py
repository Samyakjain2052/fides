"""The vendor register: processors, their contracts, and their documents.

§8(2) keeps the Data Fiduciary responsible for processing carried out by its
processors, which makes every vendor holding personal data part of the
company's own compliance surface.

Revision ID: 0019_vendors
Revises: 0018_assessments
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_vendors"
down_revision: str | None = "0018_assessments"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"

TABLES = ("vendors", "vendor_documents", "vendor_systems")


def upgrade() -> None:
    op.create_table(
        "vendors",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("domain", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="prospective"),
        sa.Column("risk_tier", sa.String(8), nullable=False,
                  server_default="medium"),
        sa.Column("role", sa.String(16), nullable=False,
                  server_default="processor"),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dpa_signed", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("dpa_signed_on", sa.Date(), nullable=True),
        sa.Column("dpa_expires_on", sa.Date(), nullable=True),
        sa.Column("dsar_contact", sa.String(320), nullable=True),
        sa.Column("dsar_sla_days", sa.Integer(), nullable=True),
        sa.Column("breach_notice_hours", sa.Integer(), nullable=True),
        sa.Column("data_categories", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("data_location", sa.Text(), nullable=True),
        sa.Column("transfers_outside_india", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("subprocessors", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("certifications", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_every_days", sa.Integer(), nullable=True),
        sa.Column("next_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_vendors"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_vendors_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"],
                                ondelete="SET NULL",
                                name="fk_vendors_owner_user_id_users"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL",
                                name="fk_vendors_created_by_users"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_vendors_tenant_name"),
        sa.CheckConstraint(
            "status IN ('prospective','approved','conditional','rejected',"
            "'retired')",
            name="ck_vendors_status",
        ),
        sa.CheckConstraint(
            "risk_tier IN ('low','medium','high','critical')",
            name="ck_vendors_risk_tier",
        ),
        sa.CheckConstraint(
            "role IN ('processor','sub_processor','joint','recipient')",
            name="ck_vendors_role",
        ),
        sa.CheckConstraint(
            "status <> 'rejected' OR decision_note IS NOT NULL",
            name="ck_vendors_rejected_has_reason",
        ),
        sa.CheckConstraint(
            "status <> 'conditional' OR decision_note IS NOT NULL",
            name="ck_vendors_conditional_has_conditions",
        ),
        sa.CheckConstraint(
            "dsar_sla_days IS NULL OR dsar_sla_days > 0",
            name="ck_vendors_sla_positive",
        ),
        sa.CheckConstraint(
            "review_every_days IS NULL OR review_every_days > 0",
            name="ck_vendors_cadence_positive",
        ),
        sa.CheckConstraint(
            "NOT dpa_signed OR dpa_signed_on IS NOT NULL",
            name="ck_vendors_dpa_has_date",
        ),
    )
    op.create_index("ix_vendors_tenant_id", "vendors", ["tenant_id"])
    op.create_index("ix_vendors_created_at", "vendors", ["created_at"])
    op.create_index("ix_vendors_tenant_status", "vendors",
                    ["tenant_id", "status"])
    op.create_index("ix_vendors_tenant_tier", "vendors",
                    ["tenant_id", "risk_tier"])
    op.create_index("ix_vendors_review", "vendors",
                    ["tenant_id", "next_review_at"])

    op.create_table(
        "vendor_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("changed_since_review", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_vendor_documents"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_vendor_documents_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="CASCADE",
                                name="fk_vendor_documents_vendor_id_vendors"),
        sa.ForeignKeyConstraint(["file_id"], ["stored_files.id"],
                                ondelete="SET NULL",
                                name="fk_vendor_documents_file_id_stored_files"),
        sa.CheckConstraint(
            "kind IN ('privacy_policy','dpa','subprocessor_list','certification',"
            "'security_report','breach_notice','other')",
            name="ck_vendor_documents_kind",
        ),
        sa.CheckConstraint(
            "url IS NOT NULL OR file_id IS NOT NULL",
            name="ck_vendor_documents_has_url_or_file",
        ),
    )
    op.create_index("ix_vendor_documents_tenant_id", "vendor_documents",
                    ["tenant_id"])
    op.create_index("ix_vendor_documents_created_at", "vendor_documents",
                    ["created_at"])
    op.create_index("ix_vendor_documents_vendor", "vendor_documents",
                    ["vendor_id"])

    op.create_table(
        "vendor_systems",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_vendor_systems"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_vendor_systems_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="CASCADE",
                                name="fk_vendor_systems_vendor_id_vendors"),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"],
                                ondelete="CASCADE",
                                name="fk_vendor_systems_connection_id_connections"),
        sa.UniqueConstraint("vendor_id", "connection_id",
                            name="uq_vendor_systems_pair"),
    )
    op.create_index("ix_vendor_systems_tenant_id", "vendor_systems",
                    ["tenant_id"])
    op.create_index("ix_vendor_systems_created_at", "vendor_systems",
                    ["created_at"])
    op.create_index("ix_vendor_systems_connection", "vendor_systems",
                    ["connection_id"])

    for table in TABLES:
        # DELETE granted: a vendor entered by mistake, a document link that was
        # wrong, a system association that no longer holds. Unlike a rights
        # request, none of these is a statutory record of an act — the audit
        # chain keeps the decisions, and the register is a working inventory.
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
