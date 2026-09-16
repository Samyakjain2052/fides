"""Outbound alerts to fiduciaries and processors, and the log that proves delivery.

A withdrawn consent changed a row and reached nothing. §6(6) gives the right to
withdraw at any time and the consequence is that processing ceases — which a
consent ledger cannot bring about on its own if no system doing the processing is
ever told. MeitY's BRD names this in §4.4.2 and repeats it as "Real-Time
Synchronization" in §4.1.1, §4.1.3 and §4.1.5.

Two tables rather than one, because delivery is a queue and the log is evidence,
and those have different write patterns: the queue is claimed, updated and
retried, while the record of "this processor was told on the 14th and confirmed
on the 14th" has to survive somebody's interest in it being untrue.

Revision ID: 0023_webhooks
Revises: 0022_inbound_rights_email
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023_webhooks"
down_revision: str | None = "0022_inbound_rights_email"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"

TABLES = ("webhook_endpoints", "webhook_deliveries")


def upgrade() -> None:
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # JSONB rather than a join table. The list is short, closed, and always
        # read whole — "which events does this endpoint want" has no query that
        # would benefit from rows, and `events @> '["consent.withdrawn"]'` is
        # exactly the lookup the emitter does.
        sa.Column("events", postgresql.JSONB(), nullable=False),
        sa.Column("secret_sealed", sa.Text(), nullable=False),
        sa.Column("secret_hint", sa.String(16), nullable=False),
        sa.Column("secret_rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_reason", sa.Text(), nullable=True),
        sa.Column("ack_deadline_hours", sa.Integer(), nullable=False,
                  server_default="24"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        # https only, enforced by the database and not merely by the service.
        # These bodies carry a person's identifier and an instruction about their
        # data; a row that got here past a future code path with the check
        # missing would put both on the wire in clear.
        sa.CheckConstraint("url LIKE 'https://%'", name="https_only"),
    )
    op.create_index("ix_webhook_endpoints_created_at", "webhook_endpoints",
                    ["created_at"])
    op.create_index("ix_webhook_endpoints_tenant_id_active", "webhook_endpoints",
                    ["tenant_id", "active"])

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledgement_note", sa.String(500), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entity_type", sa.String(32), nullable=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["endpoint_id"], ["webhook_endpoints.id"],
                                ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('queued','sending','delivered','acknowledged','failed')",
            name="known_status",
        ),
        # Acknowledgement is the receiver's claim that it acted, and it can only
        # follow our having delivered. A row acknowledged but never delivered
        # would be evidence of something that did not happen.
        sa.CheckConstraint(
            "acknowledged_at IS NULL OR delivered_at IS NOT NULL",
            name="ack_implies_delivered",
        ),
    )
    op.create_index("ix_webhook_deliveries_created_at", "webhook_deliveries",
                    ["created_at"])
    op.create_index("ix_webhook_deliveries_status", "webhook_deliveries", ["status"])
    op.create_index("ix_webhook_deliveries_endpoint_id_created_at",
                    "webhook_deliveries", ["endpoint_id", "created_at"])
    # The drain query. Partial, because delivered and acknowledged rows are the
    # overwhelming majority and never match it — without the predicate this index
    # grows with total history rather than with the depth of the queue.
    op.execute(
        """
        CREATE INDEX ix_webhook_deliveries_due
          ON webhook_deliveries (next_attempt_at)
          WHERE status IN ('queued','sending')
        """
    )
    # The escalation sweep, and the console's "who has not confirmed" view.
    op.execute(
        """
        CREATE INDEX ix_webhook_deliveries_unacked
          ON webhook_deliveries (tenant_id, delivered_at)
          WHERE status = 'delivered'
        """
    )

    for table in TABLES:
        # DELETE granted on endpoints so a decommissioned processor can be
        # removed. Note what that cascades to — see `delete_endpoint`, which
        # says why disabling is usually the right act instead.
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
    op.drop_table("webhook_deliveries")
    op.drop_table("webhook_endpoints")
