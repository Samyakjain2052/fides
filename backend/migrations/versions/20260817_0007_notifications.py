"""Notification templates and the delivery log.

Revision ID: 0007_notifications
Revises: 0006_retention
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_notifications"
down_revision: str | None = "0006_retention"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"
NEW_TENANT_TABLES = ["notification_templates", "notifications"]


def upgrade() -> None:
    op.create_table(
        "notification_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(8), nullable=False, server_default="email"),
        sa.Column("language", sa.String(32), nullable=False, server_default="English"),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_notification_templates_tenant_id_tenants"),
        sa.PrimaryKeyConstraint("id", name="pk_notification_templates"),
        sa.UniqueConstraint("tenant_id", "key", "channel", "language",
                            name="uq_notification_templates_tenant_key_channel_language"),
        sa.CheckConstraint("channel IN ('email','sms')",
                           name="ck_notification_templates_channel"),
    )
    op.create_index("ix_notification_templates_created_at", "notification_templates",
                    ["created_at"])
    op.create_index("ix_notification_templates_lookup", "notification_templates",
                    ["tenant_id", "key", "channel"])

    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_key", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(8), nullable=False, server_default="email"),
        sa.Column("language", sa.String(32), nullable=False),
        sa.Column("language_requested", sa.String(32), nullable=True),
        sa.Column("to_address", sa.String(320), nullable=False),
        sa.Column("subject_rendered", sa.String(255), nullable=False),
        sa.Column("pending_body", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("provider_message_id", sa.String(255), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("suppression_reason", sa.String(255), nullable=True),
        sa.Column("entity_type", sa.String(32), nullable=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_notifications_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["principal_id"], ["data_principals.id"],
                                ondelete="SET NULL",
                                name="fk_notifications_principal_id_data_principals"),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        # Idempotency as a constraint. A DPO refreshing a queue, or a retried
        # job, must not tell a data principal the same thing twice.
        sa.UniqueConstraint("tenant_id", "template_key", "entity_type", "entity_id",
                            name="uq_notifications_tenant_template_entity"),
        sa.CheckConstraint("channel IN ('email','sms')", name="ck_notifications_channel"),
        sa.CheckConstraint(
            "status IN ('queued','sending','delivered','failed','suppressed')",
            name="ck_notifications_status",
        ),
        # A failure has to say what went wrong, and a suppression has to say why
        # it was deliberate. "Not sent" with no explanation is the same
        # indefensible silence as a rejection with no reason.
        sa.CheckConstraint(
            "status <> 'failed' OR last_error IS NOT NULL",
            name="ck_notifications_failed_has_error",
        ),
        sa.CheckConstraint(
            "status <> 'suppressed' OR suppression_reason IS NOT NULL",
            name="ck_notifications_suppressed_has_reason",
        ),
        sa.CheckConstraint(
            "status <> 'delivered' OR sent_at IS NOT NULL",
            name="ck_notifications_delivered_has_sent_at",
        ),
        # The body is allowed to exist only while the message is still in flight.
        # Enforced here rather than in the service because "we do not keep message
        # bodies" is a claim about the data at rest, and a claim the database
        # refuses to break is worth more than one the application intends to keep.
        sa.CheckConstraint(
            "status IN ('queued','sending') OR pending_body IS NULL",
            name="ck_notifications_no_body_once_settled",
        ),
    )
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])
    op.create_index("ix_notifications_claim", "notifications",
                    ["status", "next_attempt_at"])
    op.create_index("ix_notifications_tenant_created", "notifications",
                    ["tenant_id", "created_at"])
    op.create_index("ix_notifications_principal", "notifications",
                    ["tenant_id", "principal_id"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON notification_templates TO {APP_ROLE}")
    # The delivery log is evidence. INSERT and SELECT, plus UPDATE so a worker can
    # record the outcome of its own attempt. No DELETE: trimming the log by
    # retention runs as the owner in a scheduled job, not from the application.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON notifications TO {APP_ROLE}")

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
    op.drop_table("notifications")
    op.drop_table("notification_templates")
