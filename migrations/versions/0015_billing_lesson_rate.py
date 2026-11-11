"""add billing.lesson and billing.rate

Revision ID: 0015_billing_lesson_rate
Revises: 0014_link_code_purpose
Create Date: 2026-11-11 00:00:00.000000

The ``billing`` schema is a model package, so ``migrations/env.py`` creates it on every run (``get_schemata``); this
revision only fills it, and its downgrade leaves the schema behind, empty, like every other schema of today's models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0015_billing_lesson_rate"
down_revision: str | None = "0014_link_code_purpose"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LESSON_STATUS = postgresql.ENUM(
    "planned", "held", "cancelled", "not_held", name="lesson_status", schema="billing", create_type=False
)


def upgrade() -> None:
    op.execute(sa.text("CREATE TYPE billing.lesson_status AS ENUM ('planned', 'held', 'cancelled', 'not_held')"))
    op.create_table(
        "rate",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("student_party_id", sa.UUID(), nullable=False),
        sa.Column("cents_per_hour", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("cents_per_hour >= 0 AND cents_per_hour % 4 = 0", name="ck_rate_quarter_hour_cents"),
        sa.ForeignKeyConstraint(["student_party_id"], ["core.party.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("student_party_id", "valid_from", name="uq_rate_student_valid_from"),
        schema="billing",
    )
    op.create_table(
        "lesson",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("student_party_id", sa.UUID(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("status", _LESSON_STATUS, server_default=sa.text("'planned'"), nullable=False),
        sa.Column("billable", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("rate_cents_per_hour", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("minutes > 0 AND minutes % 15 = 0", name="ck_lesson_quarter_hour_minutes"),
        sa.CheckConstraint(
            "rate_cents_per_hour >= 0 AND rate_cents_per_hour % 4 = 0", name="ck_lesson_quarter_hour_cents"
        ),
        sa.CheckConstraint("NOT billable OR status IN ('held', 'cancelled')", name="ck_lesson_billable_status"),
        sa.ForeignKeyConstraint(["student_party_id"], ["core.party.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("student_party_id", "starts_at", name="uq_lesson_student_start"),
        schema="billing",
    )


def downgrade() -> None:
    op.drop_table("lesson", schema="billing")
    op.drop_table("rate", schema="billing")
    op.execute(sa.text("DROP TYPE billing.lesson_status"))
