"""Health tracking for connections: failure streaks and a check schedule.

Revision ID: 0013_connection_health
Revises: 0012_connections
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013_connection_health"
down_revision: str | None = "0012_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A streak, not a boolean. One failed check is a blip — a DNS hiccup, a
    # failover, a restart. Three in a row is a broken integration, and the
    # difference decides whether anybody should be told.
    op.add_column(
        "connections",
        sa.Column("consecutive_failures", sa.Integer(), nullable=False,
                  server_default="0"),
    )
    # When it last actually worked, kept separately from last_tested_at so
    # "failing since Tuesday" is answerable. last_tested_at alone cannot say it.
    op.add_column(
        "connections",
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Whether the background job should check this one. An admin who knows a
    # system is down for maintenance can stop the alerts without deleting the
    # credential and re-entering it afterwards.
    op.add_column(
        "connections",
        sa.Column("monitor", sa.Boolean(), nullable=False, server_default="true"),
    )
    # Set when a failure notification goes out, so the streak crossing the
    # threshold sends once rather than every fifteen minutes forever.
    op.add_column(
        "connections",
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # The job claims work with this: rows never checked, or checked longest ago.
    op.create_index(
        "ix_connections_monitor_last_tested",
        "connections",
        ["monitor", "last_tested_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_connections_monitor_last_tested", table_name="connections")
    for column in ("alerted_at", "monitor", "last_ok_at", "consecutive_failures"):
        op.drop_column("connections", column)
