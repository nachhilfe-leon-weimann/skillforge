"""add system.job

Revision ID: 0020_system_job
Revises: 0019_retire_geo
Create Date: 2026-11-28 00:00:00.000000

SkillForge's own job queue (ADR 0011, point 1; calendar-categories spec, P0-1). The claim reads two partial indexes,
never the finished jobs: ``ix_job_claimable`` for the due pending jobs, ``ix_job_lease`` for the running jobs whose
lease passed. ``uq_job_pending_dedupe`` keeps one pending job per kind and key - the arbiter of the queue's
``ON CONFLICT``, so its predicate must stay the one ``app/services/jobs/queue.py`` names.

Downgrade: the table and its enum type; the jobs it held are gone.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0020_system_job"
down_revision: str | None = "0019_retire_geo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "succeeded", "failed", name="job_status", schema="system"),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("attempts >= 0 AND max_attempts >= 1 AND attempts <= max_attempts", name="ck_job_attempts"),
        sa.CheckConstraint("(finished_at IS NOT NULL) = (status IN ('succeeded', 'failed'))", name="ck_job_finished"),
        sa.CheckConstraint("(locked_until IS NOT NULL) = (status = 'running')", name="ck_job_lease"),
        sa.PrimaryKeyConstraint("id"),
        schema="system",
    )
    op.create_index(
        "ix_job_claimable",
        "job",
        ["available_at"],
        unique=False,
        schema="system",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_job_lease",
        "job",
        ["locked_until"],
        unique=False,
        schema="system",
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "uq_job_pending_dedupe",
        "job",
        ["kind", "dedupe_key"],
        unique=True,
        schema="system",
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("uq_job_pending_dedupe", table_name="job", schema="system")
    op.drop_index("ix_job_lease", table_name="job", schema="system")
    op.drop_index("ix_job_claimable", table_name="job", schema="system")
    op.drop_table("job", schema="system")
    # Drop the enum type too, so the revision can be applied again after a downgrade.
    op.execute(sa.text("DROP TYPE system.job_status"))
