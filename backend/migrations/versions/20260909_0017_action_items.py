"""Per-system action items, and an owner on every data store.

The two go together: an item is assigned to somebody, and the sensible default
assignee is whoever owns the system it concerns. Without owners the fan-out can
only ever produce a pile of unassigned work.

Revision ID: 0017_action_items
Revises: 0016_dsar_fulfilment
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_action_items"
down_revision: str | None = "0016_dsar_fulfilment"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"


def upgrade() -> None:
    # ------------------------------------------------- data-store owners --
    #
    # Who is answerable for this system. Nullable, because a customer who has
    # not assigned owners yet should not be blocked from connecting anything —
    # but an unowned system is visible as unowned rather than quietly nobody's.
    op.add_column(
        "connections",
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # A second owner who is not a console user — a processor's contact, a team
    # alias. Kept as plain text because they have no account here to point at.
    op.add_column(
        "connections",
        sa.Column("owner_email", sa.String(320), nullable=True),
    )
    # What the system is for, in the owner's words. Fills the gap between a
    # connection (a credential) and a processing record (a purpose).
    op.add_column(
        "connections",
        sa.Column("purpose_note", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_connections_owner_user_id_users",
        "connections", "users",
        ["owner_user_id"], ["id"], ondelete="SET NULL",
    )

    # ------------------------------------------------------ action items --
    op.create_table(
        "dsar_action_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dsar_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("system_label", sa.String(160), nullable=False),
        sa.Column("assignee_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("automated", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="pending"),
        sa.Column("outcome", sa.String(24), nullable=True),
        sa.Column("records_found", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("attestation", sa.Text(), nullable=True),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("internal_notes", sa.Text(), nullable=True),
        sa.Column("third_parties_notified", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_dsar_action_items"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_dsar_action_items_tenant_id_tenants"),
        sa.ForeignKeyConstraint(
            ["dsar_request_id"], ["dsar_requests.id"], ondelete="CASCADE",
            name="fk_dsar_action_items_dsar_request_id_dsar_requests",
        ),
        # SET NULL, not CASCADE: deleting a connection must not erase the record
        # of what was done about a rights request through it.
        sa.ForeignKeyConstraint(
            ["connection_id"], ["connections.id"], ondelete="SET NULL",
            name="fk_dsar_action_items_connection_id_connections",
        ),
        sa.ForeignKeyConstraint(
            ["assignee_user_id"], ["users.id"], ondelete="SET NULL",
            name="fk_dsar_action_items_assignee_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["completed_by"], ["users.id"], ondelete="SET NULL",
            name="fk_dsar_action_items_completed_by_users",
        ),
        sa.UniqueConstraint(
            "dsar_request_id", "connection_id",
            name="uq_dsar_action_items_request_connection",
        ),
        sa.CheckConstraint(
            "status IN ('pending','claimed','completed','failed','skipped')",
            name="ck_dsar_action_items_status",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('data_found','no_records_matched',"
            "'erased','retained','third_party_asked')",
            name="ck_dsar_action_items_outcome",
        ),
        sa.CheckConstraint(
            "status <> 'completed' OR "
            "(outcome IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_dsar_action_items_completed_has_outcome",
        ),
        sa.CheckConstraint(
            "status <> 'skipped' OR skip_reason IS NOT NULL",
            name="ck_dsar_action_items_skipped_has_reason",
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR failure_reason IS NOT NULL",
            name="ck_dsar_action_items_failed_has_reason",
        ),
        sa.CheckConstraint(
            "outcome <> 'retained' OR skip_reason IS NOT NULL",
            name="ck_dsar_action_items_retained_has_basis",
        ),
        sa.CheckConstraint(
            "records_found >= 0", name="ck_dsar_action_items_records_not_negative"
        ),
    )
    op.create_index("ix_dsar_action_items_tenant_id", "dsar_action_items",
                    ["tenant_id"])
    op.create_index("ix_dsar_action_items_created_at", "dsar_action_items",
                    ["created_at"])
    op.create_index("ix_dsar_action_items_request", "dsar_action_items",
                    ["dsar_request_id"])
    op.create_index("ix_dsar_action_items_assignee", "dsar_action_items",
                    ["tenant_id", "assignee_user_id"])
    op.create_index("ix_dsar_action_items_status", "dsar_action_items",
                    ["tenant_id", "status"])

    # No DELETE. An action item is the record of what was done — or not done —
    # about one system on one statutory request, and a queue somebody can tidy
    # up afterwards is not evidence.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON dsar_action_items TO {APP_ROLE}")
    op.execute("ALTER TABLE dsar_action_items ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE dsar_action_items FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON dsar_action_items
          USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
          WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON dsar_action_items")
    op.execute("ALTER TABLE dsar_action_items NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE dsar_action_items DISABLE ROW LEVEL SECURITY")
    op.drop_table("dsar_action_items")

    op.drop_constraint("fk_connections_owner_user_id_users", "connections")
    for column in ("purpose_note", "owner_email", "owner_user_id"):
        op.drop_column("connections", column)
