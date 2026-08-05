"""Phase 3 — the consent domain: purposes, versioned notices, principals, consents.

Beyond the four tables, this migration installs two things the application is
deliberately not trusted to enforce:

1. **RLS policies**, identical in shape to the spine's — a table holding
   customer data without a policy is the one mistake this codebase is arranged
   to make hard.

2. **A trigger that freezes a published notice.** Consent is evidence of
   agreement to a specific text. If that text can be edited after the fact, the
   evidence is worthless, and every consent recorded against it becomes a
   liability rather than an asset. Enforcing it in a service method would mean
   trusting every future code path — including a migration, a data fix, or a
   background job — to remember. The database remembers.

Revision ID: 0002_consent_core
Revises: 0001_initial_spine
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_consent_core"
down_revision: str | None = "0001_initial_spine"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"

# Only the new tables. The spine's are already covered by 0001.
NEW_TENANT_TABLES = ["purposes", "notices", "data_principals", "consents"]


def upgrade() -> None:
    # ------------------------------------------------------------- purposes --
    op.create_table(
        "purposes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("category", sa.String(128), nullable=False),
        sa.Column("is_mandatory", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("legal_basis", sa.String(32), nullable=False, server_default="consent"),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_purposes_tenant_id_tenants"),
        sa.PrimaryKeyConstraint("id", name="pk_purposes"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_purposes_tenant_id_key"),
        sa.CheckConstraint(
            "legal_basis IN ('consent','legitimate_use','legal_obligation','vital_interest')",
            name="ck_purposes_legal_basis",
        ),
    )
    op.create_index("ix_purposes_created_at", "purposes", ["created_at"])
    op.create_index("ix_purposes_tenant_id_is_active", "purposes", ["tenant_id", "is_active"])

    # -------------------------------------------------------------- notices --
    op.create_table(
        "notices",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("language", sa.String(32), nullable=False, server_default="English"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("data_collected", sa.Text(), nullable=False),
        sa.Column("user_rights", sa.Text(), nullable=False),
        sa.Column("withdrawal_policy", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_notices_tenant_id_tenants"),
        # RESTRICT: deleting a purpose must not take the agreed-to wording with it.
        sa.ForeignKeyConstraint(["purpose_id"], ["purposes.id"], ondelete="RESTRICT",
                                name="fk_notices_purpose_id_purposes"),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"], ondelete="SET NULL",
                                name="fk_notices_published_by_users"),
        sa.PrimaryKeyConstraint("id", name="pk_notices"),
        # N4 as a constraint: one row per purpose+version+language, per tenant.
        sa.UniqueConstraint("tenant_id", "purpose_id", "version", "language",
                            name="uq_notices_tenant_id_purpose_id_version_language"),
        sa.CheckConstraint("version >= 1", name="ck_notices_version_positive"),
    )
    op.create_index("ix_notices_created_at", "notices", ["created_at"])
    op.create_index("ix_notices_tenant_id_purpose_id_language", "notices",
                    ["tenant_id", "purpose_id", "language"])

    # ------------------------------------------------------ data_principals --
    op.create_table(
        "data_principals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("is_minor", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("guardian_email", sa.String(320), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_data_principals_tenant_id_tenants"),
        sa.PrimaryKeyConstraint("id", name="pk_data_principals"),
        sa.UniqueConstraint("tenant_id", "external_id",
                            name="uq_data_principals_tenant_id_external_id"),
        # A minor without a guardian contact cannot lawfully be processed under
        # DPDP §9, so the row should not exist in that shape.
        sa.CheckConstraint(
            "NOT is_minor OR guardian_email IS NOT NULL",
            name="ck_data_principals_minor_has_guardian",
        ),
    )
    op.create_index("ix_data_principals_created_at", "data_principals", ["created_at"])
    op.create_index("ix_data_principals_tenant_id_email", "data_principals",
                    ["tenant_id", "email"])

    # ------------------------------------------------------------- consents --
    op.create_table(
        "consents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose_id", postgresql.UUID(as_uuid=True), nullable=False),
        # N4: NOT NULL. A consent that cannot name the text it was given against
        # is not evidence of anything.
        sa.Column("notice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("given_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("language", sa.String(32), nullable=False, server_default="English"),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("source", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_consents_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["principal_id"], ["data_principals.id"], ondelete="CASCADE",
                                name="fk_consents_principal_id_data_principals"),
        sa.ForeignKeyConstraint(["purpose_id"], ["purposes.id"], ondelete="RESTRICT",
                                name="fk_consents_purpose_id_purposes"),
        sa.ForeignKeyConstraint(["notice_id"], ["notices.id"], ondelete="RESTRICT",
                                name="fk_consents_notice_id_notices"),
        sa.PrimaryKeyConstraint("id", name="pk_consents"),
        sa.UniqueConstraint("tenant_id", "principal_id", "purpose_id",
                            name="uq_consents_tenant_id_principal_id_purpose_id"),
        sa.CheckConstraint("status IN ('active','withdrawn','expired')",
                           name="ck_consents_status"),
        # An active consent must say when it was given, and a withdrawn one when
        # it was withdrawn. Otherwise the row cannot answer the only two
        # questions anyone will ask of it.
        sa.CheckConstraint(
            "(status <> 'active' OR given_at IS NOT NULL) AND "
            "(status <> 'withdrawn' OR withdrawn_at IS NOT NULL)",
            name="ck_consents_status_timestamps",
        ),
    )
    op.create_index("ix_consents_created_at", "consents", ["created_at"])
    op.create_index("ix_consents_tenant_id_purpose_id_status", "consents",
                    ["tenant_id", "purpose_id", "status"])
    op.create_index("ix_consents_tenant_id_expires_at", "consents",
                    ["tenant_id", "expires_at"])

    # ------------------------------------------------- a published notice is --
    # ------------------------------------------------- immutable ------------- #
    #
    # Allowed: editing a draft (published_at IS NULL), and the single transition
    # draft -> published. Blocked: any change to the agreed-to text, the
    # version, the language, or the purpose once published — and un-publishing,
    # which would be the same thing by another route.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION notices_freeze_published()
        RETURNS TRIGGER AS $$
        BEGIN
          IF OLD.published_at IS NULL THEN
            -- Still a draft: edit freely. That is what drafts are for.
            RETURN NEW;
          END IF;

          IF NEW.published_at IS NULL THEN
            RAISE EXCEPTION
              'notice % is published and cannot be un-published; issue a new version',
              OLD.id
              USING ERRCODE = 'restrict_violation';
          END IF;

          IF NEW.content           <> OLD.content
          OR NEW.data_collected    <> OLD.data_collected
          OR NEW.user_rights       <> OLD.user_rights
          OR NEW.withdrawal_policy <> OLD.withdrawal_policy
          OR NEW.version           <> OLD.version
          OR NEW.language          <> OLD.language
          OR NEW.purpose_id        <> OLD.purpose_id
          OR NEW.published_at      <> OLD.published_at
          THEN
            RAISE EXCEPTION
              'notice % is published; its wording is evidence and cannot change. '
              'Create version % instead.', OLD.id, OLD.version + 1
              USING ERRCODE = 'restrict_violation';
          END IF;

          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_notices_freeze_published
          BEFORE UPDATE ON notices
          FOR EACH ROW EXECUTE FUNCTION notices_freeze_published()
        """
    )
    # DELETE is blocked by the FK from consents (RESTRICT) for any notice that
    # has been consented to. A published notice with no consents may still be
    # deleted — nobody relied on it, and keeping dead drafts forever is not a
    # compliance requirement.

    # ------------------------------------------------------------- grants ----
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON "
        f"purposes, notices, data_principals, consents TO {APP_ROLE}"
    )

    # ------------------------------------------------- row level security ----
    for table in NEW_TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE so the policy applies to the owner too — see 0001 for why.
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

    op.execute("DROP TRIGGER IF EXISTS trg_notices_freeze_published ON notices")
    op.execute("DROP FUNCTION IF EXISTS notices_freeze_published()")

    op.drop_table("consents")
    op.drop_table("data_principals")
    op.drop_table("notices")
    op.drop_table("purposes")
