"""DSAR requests and their timeline — the record behind the engine.

Revision ID: 0005_dsar_requests
Revises: 0004_publishable_keys
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_dsar_requests"
down_revision: str | None = "0004_publishable_keys"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"
NEW_TENANT_TABLES = ["dsar_requests", "dsar_events"]


def upgrade() -> None:
    op.create_table(
        "dsar_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reference", sa.String(32), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="received"),
        sa.Column("engine_ref", sa.String(128), nullable=True),
        sa.Column("engine_status", sa.String(32), nullable=True),
        sa.Column("engine_error", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_method", sa.String(32), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_by_actor", sa.String(16), nullable=False,
                  server_default="principal"),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("correction_payload", postgresql.JSONB(), nullable=True),
        sa.Column("package_available_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_dsar_requests_tenant_id_tenants"),
        # RESTRICT, not CASCADE: a rights request is evidence that a person
        # exercised a right. Deleting the principal must not take the proof that
        # they asked with it.
        sa.ForeignKeyConstraint(["principal_id"], ["data_principals.id"], ondelete="RESTRICT",
                                name="fk_dsar_requests_principal_id_data_principals"),
        sa.PrimaryKeyConstraint("id", name="pk_dsar_requests"),
        sa.UniqueConstraint("tenant_id", "reference",
                            name="uq_dsar_requests_tenant_reference"),
        sa.CheckConstraint("type IN ('access','erasure','correction')",
                           name="ck_dsar_requests_type"),
        sa.CheckConstraint(
            "status IN ('received','verifying','in_progress','completed',"
            "'rejected','cancelled')",
            name="ck_dsar_requests_status",
        ),
        sa.CheckConstraint("requested_by_actor IN ('principal','staff')",
                           name="ck_dsar_requests_requested_by"),
        # A completed request must say WHEN, a rejected one must say WHY.
        # Enforced here rather than in a service because a rejection with no
        # recorded reason is not a decision anyone can defend, and the next code
        # path to write this table will not remember.
        sa.CheckConstraint(
            "status <> 'completed' OR resolved_at IS NOT NULL",
            name="ck_dsar_requests_completed_has_resolved_at",
        ),
        sa.CheckConstraint(
            "status <> 'rejected' OR (rejection_reason IS NOT NULL "
            "AND length(btrim(rejection_reason)) > 0)",
            name="ck_dsar_requests_rejected_has_reason",
        ),
        # The deadline cannot precede the request.
        sa.CheckConstraint("deadline_at > submitted_at",
                           name="ck_dsar_requests_deadline_after_submitted"),
        # Correction is the manual path and has no engine reference; access and
        # erasure are the engine's. Keeping the two apart in the schema stops a
        # correction from silently looking like an engine job that never ran.
        sa.CheckConstraint(
            "type <> 'correction' OR engine_ref IS NULL",
            name="ck_dsar_requests_correction_has_no_engine_ref",
        ),
    )
    op.create_index("ix_dsar_requests_created_at", "dsar_requests", ["created_at"])
    op.create_index("ix_dsar_requests_tenant_status", "dsar_requests", ["tenant_id", "status"])
    op.create_index("ix_dsar_requests_tenant_deadline", "dsar_requests",
                    ["tenant_id", "deadline_at"])
    op.create_index("ix_dsar_requests_principal", "dsar_requests",
                    ["tenant_id", "principal_id"])
    op.create_index("ix_dsar_requests_engine_ref", "dsar_requests", ["engine_ref"])

    op.create_table(
        "dsar_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dsar_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_label", sa.String(255), nullable=True),
        sa.Column("from_status", sa.String(16), nullable=True),
        sa.Column("to_status", sa.String(16), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("automated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_dsar_events_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["dsar_request_id"], ["dsar_requests.id"], ondelete="CASCADE",
                                name="fk_dsar_events_dsar_request_id_dsar_requests"),
        sa.PrimaryKeyConstraint("id", name="pk_dsar_events"),
    )
    op.create_index("ix_dsar_events_created_at", "dsar_events", ["created_at"])
    op.create_index("ix_dsar_events_request_at", "dsar_events",
                    ["dsar_request_id", "created_at"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE ON dsar_requests TO {APP_ROLE}")
    # No DELETE on requests: a rights request is a record that someone exercised
    # a right, and the application has no business removing it.
    # Events are append-and-read, like the audit trail and consent provenance.
    op.execute(f"GRANT SELECT, INSERT ON dsar_events TO {APP_ROLE}")

    for table in NEW_TENANT_TABLES:
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
    for table in reversed(NEW_TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_table("dsar_events")
    op.drop_table("dsar_requests")
