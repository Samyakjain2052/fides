"""Phase 4 — idempotency keys and the public-API request log.

Revision ID: 0003_public_api
Revises: 0002_consent_core
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_public_api"
down_revision: str | None = "0002_consent_core"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"
NEW_TENANT_TABLES = ["idempotency_keys", "api_request_log"]


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("endpoint", sa.String(128), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_body", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_idempotency_keys_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="CASCADE",
                                name="fk_idempotency_keys_api_key_id_api_keys"),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_keys"),
        sa.UniqueConstraint("tenant_id", "api_key_id", "key",
                            name="uq_idempotency_keys_tenant_key"),
    )
    op.create_index("ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"])
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])

    op.create_table(
        "api_request_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.String(255), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("principal_ref", sa.String(255), nullable=True),
        sa.Column("purpose_key", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_api_request_log_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="CASCADE",
                                name="fk_api_request_log_api_key_id_api_keys"),
        sa.PrimaryKeyConstraint("id", name="pk_api_request_log"),
    )
    op.create_index("ix_api_request_log_key_created", "api_request_log",
                    ["api_key_id", "created_at"])
    op.create_index("ix_api_request_log_tenant_created", "api_request_log",
                    ["tenant_id", "created_at"])

    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON idempotency_keys TO {APP_ROLE}"
    )
    # The request log is append-and-read only, like the audit trail: it is
    # evidence of what a customer's systems asked us, and evidence that can be
    # rewritten is not evidence. No UPDATE, no DELETE. Retention trimming is a
    # scheduled job running as the owner, not something the API can do.
    op.execute(f"GRANT SELECT, INSERT ON api_request_log TO {APP_ROLE}")

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
    op.drop_table("api_request_log")
    op.drop_table("idempotency_keys")
