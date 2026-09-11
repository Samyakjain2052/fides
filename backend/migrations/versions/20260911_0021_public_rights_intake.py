"""Email confirmation for publicly-raised rights requests.

A request raised through the public form has no session behind it, so the
address has to be proven before anything executes. The request is recorded and
its statutory clock starts immediately — a deadline a company could stop by
ignoring an email would not be a deadline — but nothing is looked up and
nothing is deleted until somebody shows they control the mailbox.

An unconfirmed erasure request must never delete anything. That is the whole
safety property, and this column is what makes it enforceable rather than
merely intended.

Revision ID: 0021_public_rights_intake
Revises: 0020_rights_and_nomination
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0021_public_rights_intake"
down_revision: str | None = "0020_rights_and_nomination"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keyed hash of the confirmation token, never the token. Same two-hash
    # discipline as invitations, password resets and grievance confirmation.
    op.add_column(
        "dsar_requests",
        sa.Column("verification_token_hash", sa.String(64), nullable=True),
    )
    # Distinguishes a request that arrived through the unauthenticated public
    # form from one raised in the portal by somebody already signed in. It
    # matters for triage: the second has a session behind it, and the first is
    # a claim about an address until it is confirmed.
    op.add_column(
        "dsar_requests",
        sa.Column(
            "arrived_publicly", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
    )
    # The confirmation lookup happens with no tenant context — the token is all
    # the caller has — so the index has to be unique across the whole table for
    # the match to be unambiguous. Partial, because only public requests have
    # one.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_dsar_requests_verification_token
          ON dsar_requests (verification_token_hash)
          WHERE verification_token_hash IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_dsar_requests_verification_token")
    op.drop_column("dsar_requests", "arrived_publicly")
    op.drop_column("dsar_requests", "verification_token_hash")
