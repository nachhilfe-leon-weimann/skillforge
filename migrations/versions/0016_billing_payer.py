"""add billing.lesson.payer_party_id and billing.manual_payer

Revision ID: 0016_billing_payer
Revises: 0015_billing_lesson_rate
Create Date: 2026-11-11 00:00:00.000000

A lesson stores its payer when it becomes billable (billing spec, PD1); a lesson that exists already gets none here -
the preview blocks a billable lesson without a payer (`payer_missing`), and `resolve-payer` stores one. Payers billed
by hand are marked in `billing.manual_payer` (PD5). Both reference the party with RESTRICT (PD9).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016_billing_payer"
down_revision: str | None = "0015_billing_lesson_rate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("lesson", sa.Column("payer_party_id", sa.UUID(), nullable=True), schema="billing")
    op.create_foreign_key(
        "lesson_payer_party_id_fkey",
        "lesson",
        "party",
        ["payer_party_id"],
        ["id"],
        source_schema="billing",
        referent_schema="core",
        ondelete="RESTRICT",
    )
    op.create_index("ix_lesson_payer_party_id", "lesson", ["payer_party_id"], unique=False, schema="billing")
    op.create_table(
        "manual_payer",
        sa.Column("party_id", sa.UUID(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["party_id"], ["core.party.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("party_id"),
        schema="billing",
    )


def downgrade() -> None:
    op.drop_table("manual_payer", schema="billing")
    op.drop_index("ix_lesson_payer_party_id", table_name="lesson", schema="billing")
    op.drop_column("lesson", "payer_party_id", schema="billing")
