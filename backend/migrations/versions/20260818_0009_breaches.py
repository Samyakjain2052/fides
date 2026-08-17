"""The breach register, the affected-principal list, and the timeline.

Revision ID: 0009_breaches
Revises: 0008_grievances
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_breaches"
down_revision: str | None = "0008_grievances"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"
NEW_TENANT_TABLES = ["breaches", "breach_affected_principals", "breach_events"]


def upgrade() -> None:
    op.create_table(
        "breaches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reference", sa.String(32), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False, server_default="medium"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        # The statutory clock: when the fiduciary became aware.
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("contained_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "categories_affected", postgresql.ARRAY(sa.String(128)),
            nullable=False, server_default="{}",
        ),
        sa.Column("estimated_affected_count", sa.Integer(), nullable=True),
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("remediation", sa.Text(), nullable=True),
        sa.Column("board_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("board_reference", sa.String(255), nullable=True),
        sa.Column("board_submitted_by", sa.String(255), nullable=True),
        sa.Column("principals_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notification_exemption", sa.Text(), nullable=True),
        sa.Column("void_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_breaches_tenant_id_tenants"),
        sa.PrimaryKeyConstraint("id", name="pk_breaches"),
        sa.UniqueConstraint("tenant_id", "reference", name="uq_breaches_tenant_reference"),
        # Named bare so the metadata naming convention produces ck_breaches_<name>.
        sa.CheckConstraint("severity IN ('low','medium','high','critical')",
                           name="severity"),
        sa.CheckConstraint(
            "status IN ('draft','investigating','contained','notified','closed','void')",
            name="status",
        ),
        # `discovered_at` is what the whole §8(6) obligation hangs on. Optional
        # only while the entry is still a draft.
        sa.CheckConstraint(
            "status = 'draft' OR discovered_at IS NOT NULL",
            name="discovered_at_required_outside_draft",
        ),
        # THE constraint of this migration. Notifying the Board is half the duty;
        # the affected people are the other half. Without this the UI could mark a
        # breach "notified" having done one of them, producing a confident record
        # of compliance a regulator can disprove in a single question.
        sa.CheckConstraint(
            "status <> 'notified' OR "
            "(board_notified_at IS NOT NULL AND principals_notified_at IS NOT NULL)",
            name="notified_means_both",
        ),
        sa.CheckConstraint(
            "status <> 'closed' OR (root_cause IS NOT NULL AND remediation IS NOT NULL "
            "AND closed_at IS NOT NULL)",
            name="closed_needs_cause_and_fix",
        ),
        sa.CheckConstraint("status <> 'void' OR void_reason IS NOT NULL",
                           name="void_needs_reason"),
        sa.CheckConstraint(
            "(board_notified_at IS NULL) = (board_submitted_by IS NULL)",
            name="board_submission_is_whole",
        ),
        sa.CheckConstraint(
            "occurred_at IS NULL OR discovered_at IS NULL OR occurred_at <= discovered_at",
            name="occurred_before_discovered",
        ),
        sa.CheckConstraint(
            "estimated_affected_count IS NULL OR estimated_affected_count >= 0",
            name="affected_count_not_negative",
        ),
    )
    op.create_index("ix_breaches_created_at", "breaches", ["created_at"])
    op.create_index("ix_breaches_tenant_status", "breaches", ["tenant_id", "status"])
    op.create_index("ix_breaches_tenant_discovered", "breaches",
                    ["tenant_id", "discovered_at"])

    op.create_table(
        "breach_affected_principals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("breach_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suppressed_reason", sa.String(255), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="query"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_breach_affected_principals_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["breach_id"], ["breaches.id"], ondelete="CASCADE",
                                name="fk_breach_affected_principals_breach_id_breaches"),
        # RESTRICT: a person attached to a breach must not be deletable out from
        # under the record that says they were affected.
        sa.ForeignKeyConstraint(
            ["principal_id"], ["data_principals.id"], ondelete="RESTRICT",
            name="fk_breach_affected_principals_principal_id_data_principals",
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"], ["notifications.id"], ondelete="SET NULL",
            name="fk_breach_affected_principals_notification_id_notifications",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_breach_affected_principals"),
        # Attaching somebody twice would double-notify them and inflate the count
        # a regulator is shown.
        sa.UniqueConstraint("breach_id", "principal_id",
                            name="uq_breach_affected_breach_principal"),
        sa.CheckConstraint(
            "notified_at IS NULL OR notification_id IS NOT NULL "
            "OR suppressed_reason IS NOT NULL",
            name="notified_has_a_trace",
        ),
    )
    op.create_index("ix_breach_affected_created_at", "breach_affected_principals",
                    ["created_at"])
    # The resume query: rows on this breach not yet notified.
    op.create_index("ix_breach_affected_breach", "breach_affected_principals",
                    ["breach_id", "notified_at"])
    op.create_index("ix_breach_affected_tenant", "breach_affected_principals",
                    ["tenant_id"])

    op.create_table(
        "breach_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("breach_id", postgresql.UUID(as_uuid=True), nullable=False),
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
                                name="fk_breach_events_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["breach_id"], ["breaches.id"], ondelete="CASCADE",
                                name="fk_breach_events_breach_id_breaches"),
        sa.PrimaryKeyConstraint("id", name="pk_breach_events"),
    )
    op.create_index("ix_breach_events_created_at", "breach_events", ["created_at"])
    op.create_index("ix_breach_events_breach_at", "breach_events",
                    ["breach_id", "created_at"])

    # No DELETE anywhere. A register whose entries can vanish is not a register —
    # a mistaken entry becomes `void` with a reason and stays.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON breaches TO {APP_ROLE}")
    # UPDATE so a bulk run can stamp `notified_at` on its own rows as it goes.
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON breach_affected_principals TO {APP_ROLE}"
    )
    # Append and read only: nothing here records the outcome of its own attempt,
    # so there is no legitimate reason to change a row once written.
    op.execute(f"GRANT SELECT, INSERT ON breach_events TO {APP_ROLE}")

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
    op.drop_table("breach_events")
    op.drop_table("breach_affected_principals")
    op.drop_table("breaches")
