"""add billing.calendar_event and lesson.calendar_event_id

Revision ID: 0018_billing_calendar_event
Revises: 0017_billing_calendar_alias
Create Date: 2026-11-11 00:00:00.000000

One occurrence of the lesson calendar as the import last saw it, and the lesson's link to it (billing spec, SF4-3).
A lesson references its occurrence with RESTRICT: only the import deletes one, and its lesson first.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018_billing_calendar_event"
down_revision: str | None = "0017_billing_calendar_alias"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calendar_event",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title_subject", sa.Text(), nullable=True),
        sa.Column("title_name", sa.Text(), nullable=True),
        sa.Column("cancelled", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("dismissed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("changed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "reason IN ('all_day', 'phone', 'first_meeting', 'no_title_pattern', 'duration', 'unmatched', 'ambiguous')",
            name="ck_calendar_event_reason",
        ),
        sa.CheckConstraint(
            "title_name IS NULL"
            " OR (NOT dismissed AND reason IS NOT NULL AND reason IN ('duration', 'unmatched', 'ambiguous'))",
            name="ck_calendar_event_title_name",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id"),
        schema="billing",
    )
    op.create_index("ix_calendar_event_starts_at", "calendar_event", ["starts_at"], unique=False, schema="billing")
    op.add_column("lesson", sa.Column("calendar_event_id", sa.UUID(), nullable=True), schema="billing")
    op.create_unique_constraint("lesson_calendar_event_id_key", "lesson", ["calendar_event_id"], schema="billing")
    op.create_foreign_key(
        "lesson_calendar_event_id_fkey",
        "lesson",
        "calendar_event",
        ["calendar_event_id"],
        ["id"],
        source_schema="billing",
        referent_schema="billing",
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_column("lesson", "calendar_event_id", schema="billing")
    op.drop_index("ix_calendar_event_starts_at", table_name="calendar_event", schema="billing")
    op.drop_table("calendar_event", schema="billing")
