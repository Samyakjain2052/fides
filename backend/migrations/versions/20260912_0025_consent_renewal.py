"""Consent renewal: remind before the lapse, announce after it.

BRD §4.1.4 asks for a reminder before expiry and a renewal that is no harder
than the original consent. The renewal itself needed no schema — re-granting
already re-points at the current notice and re-stamps `given_at`, which is the
correct shape for a fresh act of consent. What was missing is the asking.

WHAT THESE COLUMNS ARE NOT
They are not a cached expiry state. Expiry in this product is computed against
the clock on every read, deliberately, so that a consent's validity never
depends on whether a background worker ran. Nothing that writes these columns
touches `status`. They record only what has been said to whom, so that a daily
job sends one reminder rather than thirty.

Revision ID: 0025_consent_renewal
Revises: 0024_mfa
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0025_consent_renewal"
down_revision: str | None = "0024_mfa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "consents",
        sa.Column("renewal_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "consents",
        sa.Column("expiry_announced_at", sa.DateTime(timezone=True), nullable=True),
    )

    # The reminder sweep: active consents with an expiry that has not yet been
    # reminded about. Partial on the null check, because once a workspace has
    # been running a while the overwhelming majority of rows have been stamped
    # and will never match again — an unpartitioned index would grow with total
    # history rather than with the work outstanding.
    op.execute(
        """
        CREATE INDEX ix_consents_renewal_due
          ON consents (tenant_id, expires_at)
          WHERE status = 'active'
            AND expires_at IS NOT NULL
            AND renewal_notified_at IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_consents_expiry_unannounced
          ON consents (tenant_id, expires_at)
          WHERE status = 'active'
            AND expires_at IS NOT NULL
            AND expiry_announced_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_consents_expiry_unannounced")
    op.execute("DROP INDEX IF EXISTS ix_consents_renewal_due")
    op.drop_column("consents", "expiry_announced_at")
    op.drop_column("consents", "renewal_notified_at")
