"""Connections to a customer's own systems, with encrypted credentials.

Revision ID: 0012_connections
Revises: 0011_job_runs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_connections"
down_revision: str | None = "0011_job_runs"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"


def upgrade() -> None:
    op.create_table(
        "connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connector_id", sa.String(64), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="unverified"),
        # The credential. Text rather than bytea: it is a scheme-prefixed base64
        # string, so it stays greppable for its scheme during a key rotation
        # without decoding anything.
        sa.Column("config_sealed", sa.Text(), nullable=False),
        sa.Column("config_public", postgresql.JSONB(), nullable=False,
                  server_default="{}"),
        sa.Column("hints", postgresql.JSONB(), nullable=False,
                  server_default="{}"),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_test_message", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_connections"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_connections_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL",
            name="fk_connections_created_by_users",
        ),
        sa.CheckConstraint(
            "status IN ('unverified','connected','failing','disabled')",
            name="ck_connections_status",
        ),
        sa.UniqueConstraint("tenant_id", "connector_id", "label",
                            name="uq_connections_tenant_connector_label"),
        # Belt and braces on the thing that matters most about this table: a row
        # whose credential is empty is a row that would fail confusingly later,
        # and an empty string is not a credential.
        sa.CheckConstraint("length(config_sealed) > 0",
                           name="ck_connections_sealed_not_empty"),
    )
    op.create_index("ix_connections_connector_id", "connections", ["connector_id"])
    op.create_index("ix_connections_tenant_id", "connections", ["tenant_id"])

    # DELETE is granted here, unlike the append-only evidence tables.
    #
    # A connection is not evidence — it is live configuration holding a
    # customer's production secret. When they remove an integration, the
    # credential must actually leave the database; keeping it "for the audit
    # trail" would mean retaining a Razorpay key nobody intended us to hold. The
    # audit trail records that the connection was deleted, which is the part
    # worth keeping.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON connections TO {APP_ROLE}")

    op.execute("ALTER TABLE connections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE connections FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON connections
          USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
          WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON connections")
    op.execute("ALTER TABLE connections NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE connections DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_connections_tenant_id", table_name="connections")
    op.drop_index("ix_connections_connector_id", table_name="connections")
    op.drop_table("connections")
