"""Rights requests that arrived as ordinary email.

Most rights requests in the real world are an email to whatever address a
person could find. Until now those were invisible here — the clock a regulator
cares about had started and nothing in the product knew.

Revision ID: 0022_inbound_rights_email
Revises: 0021_public_rights_intake
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_inbound_rights_email"
down_revision: str | None = "0021_public_rights_intake"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"


def upgrade() -> None:
    op.create_table(
        "inbound_rights_emails",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_address", sa.String(320), nullable=False),
        sa.Column("from_name", sa.String(200), nullable=True),
        sa.Column("to_address", sa.String(320), nullable=True),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_message_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="received"),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("linked_request_id", postgresql.UUID(as_uuid=True),
                  nullable=True),
        sa.Column("dismissal_reason", sa.Text(), nullable=True),
        sa.Column("dismissed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("suspected_type", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_inbound_rights_emails"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_inbound_rights_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["linked_request_id"], ["dsar_requests.id"],
                                ondelete="SET NULL",
                                name="fk_inbound_rights_linked_request_dsar"),
        sa.ForeignKeyConstraint(["dismissed_by"], ["users.id"],
                                ondelete="SET NULL",
                                name="fk_inbound_rights_dismissed_by_users"),
        sa.CheckConstraint(
            "status IN ('received','replied','linked','dismissed')",
            name="ck_inbound_rights_emails_status",
        ),
        sa.CheckConstraint(
            "status <> 'dismissed' OR dismissal_reason IS NOT NULL",
            name="ck_inbound_rights_emails_dismissed_has_reason",
        ),
    )
    op.create_index("ix_inbound_rights_emails_tenant_id",
                    "inbound_rights_emails", ["tenant_id"])
    op.create_index("ix_inbound_rights_emails_created_at",
                    "inbound_rights_emails", ["created_at"])
    op.create_index("ix_inbound_rights_tenant_status",
                    "inbound_rights_emails", ["tenant_id", "status"])
    op.create_index("ix_inbound_rights_from",
                    "inbound_rights_emails", ["tenant_id", "from_address"])
    # Deduplicates a provider retrying a delivery. Providers retry on any
    # non-2xx, so without this one transient timeout on our side becomes three
    # copies of somebody's email in the queue.
    op.execute(
        """
        CREATE UNIQUE INDEX ix_inbound_rights_message_id
          ON inbound_rights_emails (tenant_id, provider_message_id)
          WHERE provider_message_id IS NOT NULL
        """
    )

    # No DELETE. An email asking a company to delete your data is the start of
    # a statutory clock, and a queue somebody can tidy up is not a record of
    # when they were put on notice.
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON inbound_rights_emails TO {APP_ROLE}"
    )
    op.execute("ALTER TABLE inbound_rights_emails ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE inbound_rights_emails FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON inbound_rights_emails
          USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
          WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON inbound_rights_emails")
    op.execute("ALTER TABLE inbound_rights_emails NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE inbound_rights_emails DISABLE ROW LEVEL SECURITY")
    op.drop_table("inbound_rights_emails")
