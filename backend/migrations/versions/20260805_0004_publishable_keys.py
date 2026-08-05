"""Publishable keys, consent provenance, and the per-tenant consent signing secret.

Revision ID: 0004_publishable_keys
Revises: 0003_public_api
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_publishable_keys"
down_revision: str | None = "0003_public_api"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"
NEW_TENANT_TABLES = ["publishable_keys", "consent_provenance"]


def upgrade() -> None:
    # A per-tenant secret for the signed-token step-up. On `tenants` rather than
    # on the key so that rotating a publishable key does not invalidate tokens
    # the integrator's server is already minting.
    op.add_column(
        "tenants",
        sa.Column("consent_token_secret", sa.String(128), nullable=True),
    )

    # ------------------------------------------------------ publishable keys --
    op.create_table(
        "publishable_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("prefix", sa.String(32), nullable=False),
        sa.Column("environment", sa.String(8), nullable=False, server_default="live"),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("lookup_hash", sa.String(64), nullable=False),
        sa.Column("capabilities", postgresql.ARRAY(sa.String(64)), nullable=False),
        sa.Column("allowed_origins", postgresql.ARRAY(sa.String(255)), nullable=False,
                  server_default="{}"),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("rate_limit_per_ip_per_minute", sa.Integer(), nullable=False,
                  server_default="10"),
        sa.Column("require_signed_token", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_publishable_keys_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL",
                                name="fk_publishable_keys_created_by_users"),
        sa.PrimaryKeyConstraint("id", name="pk_publishable_keys"),
        sa.UniqueConstraint("lookup_hash", name="uq_publishable_keys_lookup_hash"),
        # A publishable key may hold nothing but consent:collect, enforced by the
        # database and not only by the service that creates it. This is the one
        # constraint that stops a console bug, a data fix or a future code path
        # from putting a withdraw capability into a browser bundle.
        sa.CheckConstraint(
            "capabilities <@ ARRAY['consent:collect']::varchar(64)[]",
            name="ck_publishable_keys_collect_only",
        ),
        sa.CheckConstraint("array_length(capabilities, 1) >= 1",
                           name="ck_publishable_keys_has_capability"),
    )
    op.create_index("ix_publishable_keys_created_at", "publishable_keys", ["created_at"])
    op.create_index("ix_publishable_keys_lookup_hash", "publishable_keys", ["lookup_hash"])
    op.create_index("ix_publishable_keys_tenant_id_revoked_at", "publishable_keys",
                    ["tenant_id", "revoked_at"])

    # --------------------------------------------------- consent provenance --
    op.create_table(
        "consent_provenance",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("consent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("server_receipt_id", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collection_method", sa.String(32), nullable=False),
        sa.Column("strongly_bound", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("origin", sa.String(255), nullable=True),
        sa.Column("user_agent", sa.String(1000), nullable=True),
        sa.Column("ip_hash", sa.String(64), nullable=True),
        sa.Column("notice_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notice_version", sa.Integer(), nullable=True),
        sa.Column("publishable_key_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_consent_provenance_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["consent_id"], ["consents.id"], ondelete="CASCADE",
                                name="fk_consent_provenance_consent_id_consents"),
        sa.ForeignKeyConstraint(["notice_id"], ["notices.id"], ondelete="RESTRICT",
                                name="fk_consent_provenance_notice_id_notices"),
        sa.ForeignKeyConstraint(["publishable_key_id"], ["publishable_keys.id"],
                                ondelete="SET NULL",
                                name="fk_consent_provenance_publishable_key_id"),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="SET NULL",
                                name="fk_consent_provenance_api_key_id_api_keys"),
        sa.PrimaryKeyConstraint("id", name="pk_consent_provenance"),
        sa.UniqueConstraint("server_receipt_id", name="uq_consent_provenance_receipt"),
        sa.CheckConstraint(
            "collection_method IN "
            "('publishable_key','signed_token','session','api','import')",
            name="ck_consent_provenance_method",
        ),
        # strongly_bound is only meaningful for a signed token. Anything else
        # claiming it would be asserting a binding that was never verified.
        sa.CheckConstraint(
            "NOT strongly_bound OR collection_method = 'signed_token'",
            name="ck_consent_provenance_strong_binding_needs_token",
        ),
    )
    op.create_index("ix_consent_provenance_created_at", "consent_provenance", ["created_at"])
    op.create_index("ix_consent_provenance_consent_id", "consent_provenance", ["consent_id"])
    op.create_index("ix_consent_provenance_tenant_received", "consent_provenance",
                    ["tenant_id", "received_at"])

    # ------------------------------------- the request log serves both callers --
    # A publishable key is a distinct caller type, and the rate limiter counts by
    # whichever one made the call, so the log needs a column for each. api_key_id
    # becomes nullable for the same reason.
    op.add_column(
        "api_request_log",
        sa.Column("publishable_key_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_api_request_log_publishable_key_id", "api_request_log", "publishable_keys",
        ["publishable_key_id"], ["id"], ondelete="CASCADE",
    )
    op.alter_column("api_request_log", "api_key_id", nullable=True)
    op.create_index("ix_api_request_log_pk_created", "api_request_log",
                    ["publishable_key_id", "created_at"])
    # Per-IP limiting needs an index on the hashed IP, not the raw one.
    op.add_column("api_request_log", sa.Column("ip_hash", sa.String(64), nullable=True))
    op.create_index("ix_api_request_log_ip_created", "api_request_log",
                    ["ip_hash", "created_at"])
    op.create_check_constraint(
        "ck_api_request_log_has_a_caller",
        "api_request_log",
        "api_key_id IS NOT NULL OR publishable_key_id IS NOT NULL",
    )

    # ----------------------------------- idempotency serves both callers too --
    op.add_column(
        "idempotency_keys",
        sa.Column("publishable_key_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_idempotency_keys_publishable_key_id", "idempotency_keys",
        "publishable_keys", ["publishable_key_id"], ["id"], ondelete="CASCADE",
    )
    op.alter_column("idempotency_keys", "api_key_id", nullable=True)
    # Widen the uniqueness to include the caller type. Two credentials may
    # legitimately mint the same key value; letting one block the other would be
    # an outage with no cause visible from either side.
    op.drop_constraint("uq_idempotency_keys_tenant_key", "idempotency_keys",
                       type_="unique")
    op.create_unique_constraint(
        "uq_idempotency_keys_tenant_key", "idempotency_keys",
        ["tenant_id", "api_key_id", "publishable_key_id", "key"],
    )
    op.create_check_constraint(
        "ck_idempotency_keys_has_a_caller",
        "idempotency_keys",
        "api_key_id IS NOT NULL OR publishable_key_id IS NOT NULL",
    )

    # ---------------------------------- a new kind of actor in the audit trail --
    # `publishable_key` is deliberately distinct from `api_key`. "This came from a
    # browser banner using a published, collect-only credential" is materially
    # weaker provenance than "a server-side integration did this", and the trail
    # should not blur the two — that distinction is exactly what a reviewer needs
    # in order to weigh a record.
    op.drop_constraint("ck_audit_events_ck_audit_events_actor_type", "audit_events")
    op.create_check_constraint(
        "ck_audit_events_actor_type",
        "audit_events",
        "actor_type IN ('user','api_key','publishable_key','system','data_principal')",
    )

    # ------------------------------------------------------------- grants ----
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON publishable_keys TO {APP_ROLE}")
    # Provenance is evidence, so it is append-and-read like the audit trail.
    # Nothing in the application may rewrite where a consent came from.
    op.execute(f"GRANT SELECT, INSERT ON consent_provenance TO {APP_ROLE}")

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

    op.drop_constraint("ck_audit_events_ck_audit_events_actor_type", "audit_events")
    op.create_check_constraint(
        "ck_audit_events_actor_type",
        "audit_events",
        "actor_type IN ('user','api_key','system','data_principal')",
    )

    op.drop_constraint("ck_idempotency_keys_has_a_caller", "idempotency_keys")
    op.drop_constraint("uq_idempotency_keys_tenant_key", "idempotency_keys", type_="unique")
    op.create_unique_constraint(
        "uq_idempotency_keys_tenant_key", "idempotency_keys",
        ["tenant_id", "api_key_id", "key"],
    )
    op.alter_column("idempotency_keys", "api_key_id", nullable=False)
    op.drop_constraint("fk_idempotency_keys_publishable_key_id", "idempotency_keys")
    op.drop_column("idempotency_keys", "publishable_key_id")

    op.drop_constraint("ck_api_request_log_has_a_caller", "api_request_log")
    op.drop_index("ix_api_request_log_ip_created", "api_request_log")
    op.drop_column("api_request_log", "ip_hash")
    op.drop_index("ix_api_request_log_pk_created", "api_request_log")
    op.drop_constraint("fk_api_request_log_publishable_key_id", "api_request_log")
    op.drop_column("api_request_log", "publishable_key_id")
    op.alter_column("api_request_log", "api_key_id", nullable=False)

    op.drop_table("consent_provenance")
    op.drop_table("publishable_keys")
    op.drop_column("tenants", "consent_token_secret")
