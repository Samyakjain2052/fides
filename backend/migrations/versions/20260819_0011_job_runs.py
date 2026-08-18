"""The scheduler's run log.

Revision ID: 0011_job_runs
Revises: 0010_invitations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_job_runs"
down_revision: str | None = "0010_invitations"
branch_labels = None
depends_on = None

APP_ROLE = "datashield_app"


def upgrade() -> None:
    op.create_table(
        "job_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenants_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_job_runs"),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed','skipped_locked')",
            name="status",
        ),
        # A finished run must say when it finished. Without this, a run that
        # crashed and one that completed look the same — and telling them apart is
        # the entire reason this table exists.
        sa.CheckConstraint("status = 'running' OR finished_at IS NOT NULL",
                           name="finished_unless_running"),
        sa.CheckConstraint("status <> 'failed' OR error IS NOT NULL",
                           name="failed_has_error"),
        sa.CheckConstraint("finished_at IS NULL OR finished_at >= started_at",
                           name="finished_after_started"),
    )
    op.create_index("ix_job_runs_created_at", "job_runs", ["created_at"])
    op.create_index("ix_job_runs_job_started", "job_runs", ["job", "started_at"])

    # Deliberately NO row-level security, and no tenant_id.
    #
    # One row covers a sweep across every tenant, so it holds no tenant's data —
    # only counts, a job name and an error string. Same arrangement as `tenants`.
    # Adding a tenant column would mean either a row per tenant per tick (noise) or
    # a table that answers "which customers had overdue complaints last night",
    # which is not a question a platform log should make easy.
    #
    # No DELETE: a scheduler's history is how you notice it stopped. Trimming is a
    # scheduled task for the owner role.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON job_runs TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("job_runs")
