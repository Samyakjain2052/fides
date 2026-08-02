"""Initial spine: tenants, users, refresh tokens, API keys, audit chain + RLS.

Revision ID: 0001_initial_spine
Revises:
Create Date: 2026-08-01

This migration does three things beyond creating tables, and all three are the
security model rather than schema detail:

1. Creates the restricted application role and grants it exactly what it needs.
2. Enables ROW LEVEL SECURITY on every tenant-scoped table, with FORCE so the
   policies apply even to the table owner.
3. Makes `audit_events` append-only: revokes UPDATE/DELETE from the app role and
   installs a trigger that raises on either.
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_spine"
down_revision: str | None = None
branch_labels = None
depends_on = None

# The role the application connects as. Deliberately NOT the owner of these
# tables: Postgres exempts table owners from RLS, so an app running as owner
# would have decorative policies.
APP_ROLE = os.environ.get("DS_DB_APP_ROLE", "datashield_app")

TENANT_SCOPED_TABLES = ["users", "refresh_tokens", "api_keys", "audit_events"]


def upgrade() -> None:
    # ---------------------------------------------------------------- tables --
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("legal_name", sa.String(255)),
        sa.Column("grievance_officer_name", sa.String(255)),
        sa.Column("grievance_officer_email", sa.String(320)),
        sa.Column("default_language", sa.String(32), server_default="English", nullable=False),
        sa.Column("dsar_sla_days", sa.Integer, server_default="30", nullable=False),
        sa.Column("grievance_sla_days", sa.Integer, server_default="15", nullable=False),
        sa.Column("grievance_escalation_days", sa.Integer, server_default="10", nullable=False),
        sa.Column("require_mfa", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("is_active", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("dsar_sla_days BETWEEN 1 AND 90", name="ck_tenants_dsar_sla_range"),
        sa.CheckConstraint("grievance_sla_days BETWEEN 1 AND 90", name="ck_tenants_grievance_sla_range"),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
    )
    op.create_index("ix_tenants_created_at", "tenants", ["created_at"])

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column("mfa_enabled", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("mfa_secret", sa.String(255)),
        sa.Column("external_idp", sa.String(64)),
        sa.Column("external_idp_subject", sa.String(255)),
        sa.Column("failed_login_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("password_changed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_users_tenant_id_tenants", ondelete="CASCADE"),
        # Unique per tenant, not globally: the same person may be a user of two
        # different customers of ours.
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_id_email"),
        sa.CheckConstraint(
            "role IN ('data_principal','admin','auditor','grievance_officer')",
            name="ck_users_role_valid",
        ),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_created_at", "users", ["created_at"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False),
        sa.Column("lookup_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_reason", sa.String(64)),
        sa.Column("user_agent", sa.String(512)),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_refresh_tokens_user_id_users", ondelete="CASCADE"),
    )
    op.create_index("ix_refresh_tokens_tenant_id", "refresh_tokens", ["tenant_id"])
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.create_index("ix_refresh_tokens_lookup_hash", "refresh_tokens", ["lookup_hash"])
    op.create_index("ix_refresh_tokens_created_at", "refresh_tokens", ["created_at"])

    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("prefix", sa.String(32), nullable=False),
        sa.Column("environment", sa.String(8), server_default="live", nullable=False),
        sa.Column("key_hash", sa.String(255), nullable=False),
        sa.Column("lookup_hash", sa.String(64), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.String(64)), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_api_keys_tenant_id_tenants", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_api_keys_created_by_users", ondelete="SET NULL"),
        # Globally unique: this is the index an inbound key is looked up by, and
        # it must resolve to exactly one tenant.
        sa.UniqueConstraint("lookup_hash", name="uq_api_keys_lookup_hash"),
        sa.CheckConstraint("environment IN ('live','test')", name="ck_api_keys_environment"),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])
    op.create_index("ix_api_keys_created_at", "api_keys", ["created_at"])

    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.BigInteger, nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True)),
        sa.Column("actor_label", sa.String(255)),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(64)),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True)),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("user_agent", sa.String(512)),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
        # No updated_at, on purpose: a row that records its own modification time
        # is admitting it can be modified.
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_audit_events_tenant_id_tenants", ondelete="RESTRICT"),
        # Two entries can never claim the same position in a tenant's chain, even
        # if the advisory lock were somehow bypassed.
        sa.UniqueConstraint("tenant_id", "seq", name="uq_audit_events_tenant_id_seq"),
        sa.CheckConstraint(
            "actor_type IN ('user','api_key','system','data_principal')",
            name="ck_audit_events_actor_type",
        ),
    )
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_index("ix_audit_events_entity", "audit_events", ["tenant_id", "entity_type", "entity_id"])
    op.create_index("ix_audit_events_actor", "audit_events", ["tenant_id", "actor_id"])
    op.create_index("ix_audit_events_action_created", "audit_events", ["tenant_id", "action", "created_at"])

    # ----------------------------------------------------------- app role ----
    # The role itself is created by scripts_bootstrap.sql, which runs once as a
    # superuser at database init. Creating roles needs CREATEROLE, and migrations
    # deliberately run as a plain table owner — a migration that can rewrite role
    # attributes is a migration that can grant itself BYPASSRLS.
    #
    # So this migration VERIFIES rather than sets, and fails loudly with an
    # actionable message if the precondition is missing. A silent skip here would
    # mean shipping tables whose policies apply to nobody.
    op.execute(
        f"""
        DO $$
        DECLARE
          bypasses boolean;
        BEGIN
          SELECT rolbypassrls INTO bypasses FROM pg_roles WHERE rolname = '{APP_ROLE}';
          IF bypasses IS NULL THEN
            RAISE EXCEPTION
              'role "{APP_ROLE}" does not exist. Run backend/scripts_bootstrap.sql '
              'as a superuser before migrating.';
          END IF;
          IF bypasses THEN
            RAISE EXCEPTION
              'role "{APP_ROLE}" has BYPASSRLS, which disables every tenant '
              'isolation policy. Fix with: ALTER ROLE {APP_ROLE} NOBYPASSRLS;';
          END IF;
        END $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON tenants, users, refresh_tokens, api_keys TO {APP_ROLE}"
    )
    # The audit table is the exception: INSERT and SELECT only. No UPDATE, no
    # DELETE, for anyone using this role — which is the application.
    op.execute(f"GRANT SELECT, INSERT ON audit_events TO {APP_ROLE}")

    # ---------------------------------------------------- row level security --
    for table in TENANT_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE so the policy applies to the table owner too. Without it, anything
        # connecting as the owner (migrations, a careless ops session, a
        # misconfigured app) sees every tenant.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
              WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            """
        )
    # USING filters reads; WITH CHECK stops writes into another tenant. Both are
    # needed — USING alone would let a tenant INSERT a row belonging to someone
    # else and then be unable to see it.
    #
    # NULLIF(..., '') matters: current_setting returns '' (not NULL) for a
    # variable set to the empty string, and ''::uuid raises. This makes an unset
    # context compare against NULL, which matches nothing — failing closed.

    # `tenants` itself is deliberately NOT under RLS: authentication has to find
    # a tenant by slug before any tenant context exists. It holds no personal
    # data, only company configuration.

    # ------------------------------------------------------- append-only ------
    # Grants stop the application. This trigger stops anyone who obtains the
    # application's connection, and documents the intent in the schema itself.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_events_immutable()
        RETURNS TRIGGER AS $$
        BEGIN
          RAISE EXCEPTION
            'audit_events is append-only: % is not permitted (attempted on id %)',
            TG_OP, COALESCE(OLD.id::text, '?')
            USING ERRCODE = 'insufficient_privilege';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_no_update_delete
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION audit_events_immutable();
        """
    )

    # Chain head lookup: "the latest entry for this tenant" runs on every audit
    # write, so it gets its own descending index.
    op.execute("CREATE INDEX ix_audit_events_chain_head ON audit_events (tenant_id, seq DESC)")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_update_delete ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_immutable()")
    for table in TENANT_SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_table("audit_events")
    op.drop_table("api_keys")
    op.drop_table("refresh_tokens")
    op.drop_table("users")
    op.drop_table("tenants")
    # The role is intentionally left in place: it may own grants on other
    # databases, and dropping a role out from under them is worse than leaving it.
