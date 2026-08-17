"""Grievances and their append-only timeline.

Revision ID: 0008_grievances
Revises: 0007_notifications
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_grievances"
down_revision: str | None = "0007_notifications"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"
NEW_TENANT_TABLES = ["grievances", "grievance_events"]


def upgrade() -> None:
    op.create_table(
        "grievances",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Nullable: requiring an account would make one a precondition for
        # exercising a statutory right.
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reference", sa.String(32), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("contact_email", sa.String(320), nullable=True),
        sa.Column("contact_verified", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("verification_token_hash", sa.String(64), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("assigned_to", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("related_dsar_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("escalate_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("satisfaction_rating", sa.Integer(), nullable=True),
        sa.Column("satisfaction_comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_grievances_tenant_id_tenants"),
        # RESTRICT, matching dsar_requests: a principal with an open grievance
        # must not be deletable out from under it.
        sa.ForeignKeyConstraint(["principal_id"], ["data_principals.id"],
                                ondelete="RESTRICT",
                                name="fk_grievances_principal_id_data_principals"),
        sa.ForeignKeyConstraint(["assigned_to"], ["users.id"], ondelete="SET NULL",
                                name="fk_grievances_assigned_to_users"),
        sa.ForeignKeyConstraint(["related_dsar_id"], ["dsar_requests.id"],
                                ondelete="SET NULL",
                                name="fk_grievances_related_dsar_id_dsar_requests"),
        sa.PrimaryKeyConstraint("id", name="pk_grievances"),
        sa.UniqueConstraint("tenant_id", "reference",
                            name="uq_grievances_tenant_reference"),
        # Named bare so the metadata naming convention produces
        # `ck_grievances_<name>`. See the note in app/models/grievance.py.
        sa.CheckConstraint(
            "status IN ('open','acknowledged','in_progress','resolved',"
            "'rejected','reopened')",
            name="status",
        ),
        sa.CheckConstraint(
            "category IN ('consent_violation','data_breach','dsar_delay',"
            "'inaccurate_data','other')",
            name="category",
        ),
        # Somebody must be reachable, or this is a complaint that cannot be
        # redressed and should never have been accepted.
        sa.CheckConstraint(
            "principal_id IS NOT NULL OR contact_email IS NOT NULL",
            name="reachable",
        ),
        # The two constraints this module exists to guarantee. A resolution with
        # no record of the resolution, or a refusal with no reason, is not a
        # redressal mechanism — it is a queue that empties itself.
        sa.CheckConstraint(
            "status <> 'resolved' OR "
            "(resolution_notes IS NOT NULL AND resolved_at IS NOT NULL)",
            name="resolved_has_notes",
        ),
        sa.CheckConstraint(
            "status <> 'rejected' OR rejection_reason IS NOT NULL",
            name="rejected_has_reason",
        ),
        sa.CheckConstraint(
            "NOT escalated OR escalated_at IS NOT NULL",
            name="escalated_has_timestamp",
        ),
        sa.CheckConstraint("deadline_at > submitted_at", name="deadline_after_submit"),
        sa.CheckConstraint(
            "satisfaction_rating IS NULL OR satisfaction_rating BETWEEN 1 AND 5",
            name="rating_range",
        ),
        sa.CheckConstraint(
            "satisfaction_rating IS NULL OR status IN ('resolved','reopened')",
            name="rating_needs_resolution",
        ),
        sa.CheckConstraint(
            "NOT contact_verified OR verified_at IS NOT NULL",
            name="verified_has_timestamp",
        ),
    )
    op.create_index("ix_grievances_created_at", "grievances", ["created_at"])
    op.create_index("ix_grievances_tenant_status", "grievances", ["tenant_id", "status"])
    op.create_index("ix_grievances_tenant_deadline", "grievances",
                    ["tenant_id", "deadline_at"])
    op.create_index("ix_grievances_principal", "grievances", ["tenant_id", "principal_id"])
    op.create_index("ix_grievances_assigned", "grievances", ["tenant_id", "assigned_to"])
    # The escalation sweep's query: unescalated, still open, past the threshold.
    op.create_index("ix_grievances_escalation_sweep", "grievances",
                    ["escalated", "status", "escalate_at"])

    op.create_table(
        "grievance_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("grievance_id", postgresql.UUID(as_uuid=True), nullable=False),
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
                                name="fk_grievance_events_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["grievance_id"], ["grievances.id"], ondelete="CASCADE",
                                name="fk_grievance_events_grievance_id_grievances"),
        sa.PrimaryKeyConstraint("id", name="pk_grievance_events"),
    )
    op.create_index("ix_grievance_events_created_at", "grievance_events", ["created_at"])
    op.create_index("ix_grievance_events_grievance_at", "grievance_events",
                    ["grievance_id", "created_at"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE ON grievances TO {APP_ROLE}")
    # The timeline is evidence: append and read, nothing else. No UPDATE either —
    # unlike notifications, nothing here records the outcome of its own attempt,
    # so there is no legitimate reason to change a row once written.
    op.execute(f"GRANT SELECT, INSERT ON grievance_events TO {APP_ROLE}")

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
    op.drop_table("grievance_events")
    op.drop_table("grievances")
