"""A second factor that actually exists.

`users.mfa_enabled` and `users.mfa_secret` have been in the schema since the
first migration and nothing ever wrote to them. That is the most dangerous shape
a security control can take: a column called `mfa_enabled` that is always false
reads, to anybody skimming the schema or a security questionnaire, like a feature
that is present and switched off rather than one that was never built.

MeitY's BRD asks for MFA on admin accounts (§4.6.1) and on audit-log access
(§4.7). This adds the three columns the working implementation needs, and widens
the secret column because a sealed secret is longer than the base32 string that
was going to be stored there.

Revision ID: 0024_mfa
Revises: 0023_webhooks
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024_mfa"
down_revision: str | None = "0023_webhooks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # String(255) -> Text. The TOTP secret is SEALED rather than hashed — it is
    # the one credential here that must be recoverable, because verifying a code
    # means recomputing the HMAC from it — and AES-GCM ciphertext plus the
    # scheme prefix does not reliably fit in 255 characters.
    op.alter_column(
        "users",
        "mfa_secret",
        existing_type=sa.String(255),
        type_=sa.Text(),
        existing_nullable=True,
    )

    # The highest TOTP counter already accepted for this user.
    #
    # A code is valid across a one-step skew window, so the same six digits are
    # accepted for up to ninety seconds. That is ample time for somebody who
    # read them over a shoulder to use them again, and this column is what makes
    # a second use fail even though the digest is perfectly correct.
    op.add_column("users", sa.Column("mfa_last_counter", sa.Integer(), nullable=True))

    # Argon2 hashes of single-use recovery codes.
    #
    # Not optional. MFA with no recovery path produces permanently locked
    # accounts whose only remedy is a human at the vendor turning it off — and a
    # support process that disables MFA on request is a social-engineering
    # target that undoes the control entirely.
    op.add_column(
        "users", sa.Column("mfa_recovery_hashes", postgresql.JSONB(), nullable=True)
    )

    op.add_column(
        "users",
        sa.Column("mfa_enrolled_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Enrolment is two steps and this is the invariant that keeps them honest:
    # `mfa_enabled` may only be true once a secret exists. A row enabled without
    # one would reject every code its owner could possibly produce, and the
    # account would be unreachable by its own user.
    op.create_check_constraint(
        "mfa_enabled_needs_a_secret",
        "users",
        "NOT mfa_enabled OR mfa_secret IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_mfa_enabled_needs_a_secret", "users", type_="check")
    op.drop_column("users", "mfa_enrolled_at")
    op.drop_column("users", "mfa_recovery_hashes")
    op.drop_column("users", "mfa_last_counter")
    op.alter_column(
        "users",
        "mfa_secret",
        existing_type=sa.Text(),
        type_=sa.String(255),
        existing_nullable=True,
    )
