"""Retention policies, purge runs, and the legal hold that outranks them.

Revision ID: 0006_retention
Revises: 0005_dsar_requests
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_retention"
down_revision: str | None = "0005_dsar_requests"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"
NEW_TENANT_TABLES = ["retention_policies", "purge_runs", "purge_run_items"]


def upgrade() -> None:
    # A legal hold on the person, not just on the policy. A policy-level
    # exemption covers a category; a hold covers an individual under litigation
    # or investigation, and it has to outrank every policy that would otherwise
    # sweep them up.
    op.add_column(
        "data_principals",
        sa.Column("legal_hold", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "data_principals", sa.Column("legal_hold_reason", sa.String(255), nullable=True)
    )
    op.create_check_constraint(
        "ck_data_principals_hold_has_reason",
        "data_principals",
        "NOT legal_hold OR legal_hold_reason IS NOT NULL",
    )
    # Masking sets these to NULL, so "already purged" is a queryable state rather
    # than something inferred from the absence of an email.
    op.add_column(
        "data_principals", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_table(
        "retention_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("data_category", sa.String(128), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False, server_default="mask"),
        sa.Column("auto_delete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notify_days", sa.Integer(), nullable=False, server_default="14"),
        sa.Column("exemption_code", sa.String(32), nullable=False, server_default="none"),
        sa.Column("exemption_reference", sa.String(255), nullable=True),
        sa.Column("exemption_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_retention_policies_tenant_id_tenants"),
        sa.PrimaryKeyConstraint("id", name="pk_retention_policies"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_retention_policies_tenant_name"),
        sa.CheckConstraint("action IN ('mask','delete')", name="ck_retention_policies_action"),
        sa.CheckConstraint("retention_days >= 1", name="ck_retention_policies_days"),
        sa.CheckConstraint(
            "exemption_code IN ('none','statutory','legal_hold','dispute')",
            name="ck_retention_policies_exemption_code",
        ),
        # AUTOMATIC DESTRUCTION MUST WARN FIRST. A policy that deletes on a timer
        # with no notice period is not something the schema should permit anyone
        # to save, however they got there.
        sa.CheckConstraint(
            "NOT auto_delete OR notify_days >= 1",
            name="ck_retention_policies_auto_delete_needs_notice",
        ),
        # An exemption without a reference is an assertion, not a justification.
        sa.CheckConstraint(
            "exemption_code = 'none' OR (exemption_reference IS NOT NULL "
            "AND length(btrim(exemption_reference)) > 0)",
            name="ck_retention_policies_exemption_needs_reference",
        ),
    )
    op.create_index("ix_retention_policies_created_at", "retention_policies", ["created_at"])
    op.create_index("ix_retention_policies_tenant_active", "retention_policies",
                    ["tenant_id", "is_active"])

    op.create_table(
        "purge_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("initiated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("candidates_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_affected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scope_summary", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_purge_runs_tenant_id_tenants"),
        # RESTRICT: a policy that has destroyed data cannot be deleted out from
        # under the receipt that proves what it destroyed.
        sa.ForeignKeyConstraint(["policy_id"], ["retention_policies.id"], ondelete="RESTRICT",
                                name="fk_purge_runs_policy_id_retention_policies"),
        sa.ForeignKeyConstraint(["initiated_by"], ["users.id"], ondelete="SET NULL",
                                name="fk_purge_runs_initiated_by_users"),
        sa.PrimaryKeyConstraint("id", name="pk_purge_runs"),
        sa.CheckConstraint("mode IN ('dry_run','live')", name="ck_purge_runs_mode"),
        sa.CheckConstraint("status IN ('running','completed','failed','cancelled')",
                           name="ck_purge_runs_status"),
        # A DRY RUN CANNOT HAVE AFFECTED ANYTHING. If this constraint ever fires,
        # the preview and the executor have diverged — which is precisely the bug
        # that makes a dry run report 4 rows and the live run destroy 400.
        sa.CheckConstraint(
            "mode <> 'dry_run' OR rows_affected = 0",
            name="ck_purge_runs_dry_run_changes_nothing",
        ),
    )
    op.create_index("ix_purge_runs_created_at", "purge_runs", ["created_at"])
    op.create_index("ix_purge_runs_tenant_started", "purge_runs", ["tenant_id", "started_at"])
    op.create_index("ix_purge_runs_policy", "purge_runs", ["policy_id"])

    op.create_table(
        "purge_run_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purge_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("table_name", sa.String(64), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_taken", sa.String(16), nullable=False),
        sa.Column("skip_reason", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_purge_run_items_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["purge_run_id"], ["purge_runs.id"], ondelete="CASCADE",
                                name="fk_purge_run_items_purge_run_id_purge_runs"),
        sa.PrimaryKeyConstraint("id", name="pk_purge_run_items"),
        sa.CheckConstraint("action_taken IN ('masked','deleted','skipped')",
                           name="ck_purge_run_items_action"),
        # A skip has to say why. "Not purged" with no reason is the same
        # indefensible silence as a rejection with no reason.
        sa.CheckConstraint(
            "action_taken <> 'skipped' OR skip_reason IS NOT NULL",
            name="ck_purge_run_items_skip_has_reason",
        ),
    )
    op.create_index("ix_purge_run_items_created_at", "purge_run_items", ["created_at"])
    op.create_index("ix_purge_run_items_run", "purge_run_items", ["purge_run_id"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE ON retention_policies TO {APP_ROLE}")
    # Receipts are evidence of destruction. INSERT and SELECT only — plus UPDATE
    # on purge_runs alone, because a run has to be able to finish itself
    # (status, finished_at, counts). Items are never updated once written.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON purge_runs TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON purge_run_items TO {APP_ROLE}")

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
    op.drop_table("purge_run_items")
    op.drop_table("purge_runs")
    op.drop_table("retention_policies")
    op.drop_constraint("ck_data_principals_hold_has_reason", "data_principals")
    op.drop_column("data_principals", "purged_at")
    op.drop_column("data_principals", "legal_hold_reason")
    op.drop_column("data_principals", "legal_hold")
