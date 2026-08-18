"""User invitations, and the database half of the last-admin rule.

Revision ID: 0010_invitations
Revises: 0009_breaches
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_invitations"
down_revision: str | None = "0009_breaches"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"


def upgrade() -> None:
    op.create_table(
        "user_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        # Argon2id, like a refresh token. The raw token is shown once.
        sa.Column("token_hash", sa.String(255), nullable=False),
        # Deterministic, for the lookup — Argon2 is salted and cannot be queried.
        sa.Column("lookup_hash", sa.String(64), nullable=False),
        sa.Column("invited_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_user_invitations_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], ondelete="SET NULL",
                                name="fk_user_invitations_invited_by_users"),
        sa.ForeignKeyConstraint(["accepted_user_id"], ["users.id"], ondelete="SET NULL",
                                name="fk_user_invitations_accepted_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_user_invitations"),
        sa.CheckConstraint("accepted_at IS NULL OR revoked_at IS NULL",
                           name="not_both_accepted_and_revoked"),
        sa.CheckConstraint("revoked_at IS NULL OR revoked_reason IS NOT NULL",
                           name="revoked_needs_reason"),
        sa.CheckConstraint("expires_at > created_at", name="expires_after_created"),
    )
    op.create_index("ix_user_invitations_created_at", "user_invitations", ["created_at"])
    op.create_index("ix_user_invitations_lookup_hash", "user_invitations",
                    ["lookup_hash"])
    op.create_index("ix_user_invitations_tenant_created", "user_invitations",
                    ["tenant_id", "created_at"])
    # One LIVE invitation per address. Partial, because accepted and revoked rows
    # are kept as history and a plain UNIQUE would refuse a legitimate
    # re-invitation after a revoke.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_user_invitations_live_email
          ON user_invitations (tenant_id, email)
          WHERE accepted_at IS NULL AND revoked_at IS NULL
        """
    )

    # No DELETE. An invitation is a credential that was issued; the record that it
    # existed, and what became of it, is worth more than a clean table.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON user_invitations TO {APP_ROLE}")
    op.execute("ALTER TABLE user_invitations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_invitations FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON user_invitations
          USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
          WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )

    # ---------------------------------------------------------------- #
    # The last-admin rule, in the database
    # ---------------------------------------------------------------- #
    #
    # A workspace with no active admin is unrecoverable without support access,
    # which is the worst possible support ticket. The service refuses it with a
    # sentence somebody can act on; this trigger is what makes it true regardless
    # of which code path tries.
    #
    # A trigger rather than a CHECK because the rule is about the *set* of rows,
    # not one row — no CHECK constraint can count its own table.
    #
    # It fires on UPDATE only. An INSERT cannot remove the last admin, and DELETE
    # is not granted to the application role.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION assert_tenant_keeps_an_admin()
        RETURNS TRIGGER AS $$
        DECLARE
          remaining integer;
        BEGIN
          -- Only when this row is ceasing to be an active admin.
          IF (OLD.role = 'admin' AND OLD.is_active)
             AND (NEW.role <> 'admin' OR NOT NEW.is_active) THEN
            SELECT count(*) INTO remaining
              FROM users
             WHERE tenant_id = OLD.tenant_id
               AND role = 'admin'
               AND is_active
               AND id <> OLD.id;
            IF remaining = 0 THEN
              RAISE EXCEPTION
                'a workspace must keep at least one active admin (user %)', OLD.id
                USING ERRCODE = 'check_violation';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER users_keep_an_admin
          BEFORE UPDATE ON users
          FOR EACH ROW EXECUTE FUNCTION assert_tenant_keeps_an_admin()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS users_keep_an_admin ON users")
    op.execute("DROP FUNCTION IF EXISTS assert_tenant_keeps_an_admin()")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON user_invitations")
    op.execute("ALTER TABLE user_invitations NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_invitations DISABLE ROW LEVEL SECURITY")
    op.drop_table("user_invitations")
