"""The §12(1) rights as distinct types, and §14 nomination.

Two things:

  * `completion` and `updating` join `correction` as request types, because
    §12(1) names them separately and they are different operations on a source
    system. The old CHECK allowed only three types.
  * `nominations` — §14, the right to nominate somebody to exercise your rights
    on your death or incapacity. No GDPR or US-state equivalent, which is why
    no comparable product has it.

Plus a duplicate-detection index on rights requests.

Revision ID: 0020_rights_and_nomination
Revises: 0019_vendors
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_rights_and_nomination"
down_revision: str | None = "0019_vendors"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"


def upgrade() -> None:
    # --------------------------------------------- the §12(1) request types --
    #
    # The existing constraint allows only access / erasure / correction, and it
    # carries a DOUBLE-PREFIXED name: `ck_dsar_requests_ck_dsar_requests_type`.
    # That is the naming bug models/grievance.py documents — some earlier tables
    # passed the `ck_<table>_` prefix explicitly and NAMING_CONVENTION prepended
    # it again. Those names stay as they are elsewhere because renaming them
    # means rewriting applied migrations; here the constraint has to be replaced
    # anyway, so the replacement gets the intended name.
    op.execute(
        "ALTER TABLE dsar_requests "
        "DROP CONSTRAINT IF EXISTS ck_dsar_requests_ck_dsar_requests_type"
    )
    op.execute(
        """
        ALTER TABLE dsar_requests
          ADD CONSTRAINT ck_dsar_requests_type
          CHECK (type IN ('access','correction','completion','updating','erasure'))
        """
    )

    # The same widening for the engine-reference rule. `completion` and
    # `updating` have no engine action either — the engine only does access and
    # erasure — so an engine ref on one of them would mean a request was
    # dispatched that cannot be executed.
    op.execute(
        "ALTER TABLE dsar_requests DROP CONSTRAINT IF EXISTS "
        "ck_dsar_requests_ck_dsar_requests_correction_has_no_engine_ref"
    )
    op.execute(
        """
        ALTER TABLE dsar_requests
          ADD CONSTRAINT ck_dsar_requests_manual_has_no_engine_ref
          CHECK (
            type NOT IN ('correction','completion','updating')
            OR engine_ref IS NULL
          )
        """
    )

    # ------------------------------------------------- duplicate detection --
    #
    # Partial index over OPEN requests only. A person may legitimately raise the
    # same kind of request again a year later, so the check is "do they already
    # have one of these open" rather than "have they ever asked" — and the index
    # only covers the rows that question looks at.
    op.execute(
        """
        CREATE INDEX ix_dsar_requests_open_duplicates
          ON dsar_requests (tenant_id, principal_id, type)
          WHERE status IN ('received','verifying','in_progress')
        """
    )

    # ---------------------------------------------- nomination, and its link --
    op.create_table(
        "nominations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("nominee_name", sa.String(200), nullable=False),
        sa.Column("nominee_email", sa.String(320), nullable=False),
        sa.Column("nominee_phone", sa.String(32), nullable=True),
        sa.Column("nominee_relationship", sa.String(120), nullable=True),
        sa.Column("scope", sa.String(24), nullable=False,
                  server_default="access_only"),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="pending"),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acceptance_token_hash", sa.String(64), nullable=True),
        sa.Column("invoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invoked_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("evidence_note", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocation_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_nominations"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE",
                                name="fk_nominations_tenant_id_tenants"),
        # RESTRICT: purging a principal must not silently drop the record that
        # somebody was authorised to act for them.
        sa.ForeignKeyConstraint(["principal_id"], ["data_principals.id"],
                                ondelete="RESTRICT",
                                name="fk_nominations_principal_id_data_principals"),
        sa.ForeignKeyConstraint(["invoked_by"], ["users.id"], ondelete="SET NULL",
                                name="fk_nominations_invoked_by_users"),
        sa.CheckConstraint(
            "status IN ('pending','active','invoked','revoked','lapsed')",
            name="ck_nominations_status",
        ),
        sa.CheckConstraint(
            "scope IN ('access_only','access_and_erasure','all_rights')",
            name="ck_nominations_scope",
        ),
        # The most consequential constraint here. Invoking hands somebody else
        # the right to extract or destroy a person's entire record, so it needs
        # a named member of staff and a note saying what they saw.
        sa.CheckConstraint(
            "status <> 'invoked' OR ("
            "invoked_at IS NOT NULL AND invoked_by IS NOT NULL "
            "AND evidence_note IS NOT NULL)",
            name="ck_nominations_invoked_has_evidence",
        ),
        sa.CheckConstraint(
            "status <> 'revoked' OR revoked_at IS NOT NULL",
            name="ck_nominations_revoked_has_timestamp",
        ),
        sa.CheckConstraint(
            "length(btrim(nominee_email)) > 0",
            name="ck_nominations_nominee_reachable",
        ),
    )
    op.create_index("ix_nominations_tenant_id", "nominations", ["tenant_id"])
    op.create_index("ix_nominations_created_at", "nominations", ["created_at"])
    op.create_index("ix_nominations_principal", "nominations",
                    ["tenant_id", "principal_id"])
    op.create_index("ix_nominations_status", "nominations",
                    ["tenant_id", "status"])

    # A request raised by a nominee rather than by the person themselves. The
    # link matters for the audit trail: "who asked for this erasure" must be
    # answerable, and "the deceased" is not the answer.
    op.add_column(
        "dsar_requests",
        sa.Column("nomination_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_dsar_requests_nomination_id_nominations",
        "dsar_requests", "nominations",
        ["nomination_id"], ["id"], ondelete="SET NULL",
    )

    # No DELETE. A nomination is a standing authorisation over somebody's
    # rights; revocation is a status, not a disappearance, because "was anybody
    # ever authorised to act for this person" has to stay answerable.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON nominations TO {APP_ROLE}")
    op.execute("ALTER TABLE nominations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE nominations FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON nominations
          USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
          WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_dsar_requests_nomination_id_nominations", "dsar_requests"
    )
    op.drop_column("dsar_requests", "nomination_id")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON nominations")
    op.execute("ALTER TABLE nominations NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE nominations DISABLE ROW LEVEL SECURITY")
    op.drop_table("nominations")

    op.execute("DROP INDEX IF EXISTS ix_dsar_requests_open_duplicates")

    # Put the original constraints back, double-prefixed names and all, so a
    # downgrade returns the schema to exactly what 0019 left.
    op.execute(
        "ALTER TABLE dsar_requests DROP CONSTRAINT IF EXISTS "
        "ck_dsar_requests_manual_has_no_engine_ref"
    )
    op.execute(
        """
        ALTER TABLE dsar_requests
          ADD CONSTRAINT ck_dsar_requests_ck_dsar_requests_correction_has_no_engine_ref
          CHECK (type <> 'correction' OR engine_ref IS NULL)
        """
    )
    op.execute(
        "ALTER TABLE dsar_requests DROP CONSTRAINT IF EXISTS ck_dsar_requests_type"
    )
    op.execute(
        """
        ALTER TABLE dsar_requests
          ADD CONSTRAINT ck_dsar_requests_ck_dsar_requests_type
          CHECK (type IN ('access','erasure','correction'))
        """
    )
