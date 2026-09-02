"""Password reset tokens.

Revision ID: 0014_password_resets
Revises: 0013_connection_health
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_password_resets"
down_revision: str | None = "0013_connection_health"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"


def upgrade() -> None:
    op.create_table(
        "password_resets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False),
        sa.Column("lookup_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_ip", sa.String(64), nullable=True),
        sa.Column("requested_user_agent", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_password_resets"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_password_resets_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE",
                                name="fk_password_resets_user_id_users"),
        # The lookup index has to be unique across the whole table, not per
        # tenant: it is what a redemption searches by, and two rows sharing one
        # would make the match ambiguous.
        sa.UniqueConstraint("lookup_hash", name="uq_password_resets_lookup_hash"),
    )
    op.create_index("ix_password_resets_user_id", "password_resets", ["user_id"])

    # UPDATE so a token can be marked used and superseded. No DELETE: a spent
    # reset is a security event worth being able to see, and the row holds no
    # secret — only two hashes of one.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON password_resets TO {APP_ROLE}")

    op.execute("ALTER TABLE password_resets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE password_resets FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON password_resets
          USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
          WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON password_resets")
    op.execute("ALTER TABLE password_resets NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE password_resets DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_password_resets_user_id", table_name="password_resets")
    op.drop_table("password_resets")
