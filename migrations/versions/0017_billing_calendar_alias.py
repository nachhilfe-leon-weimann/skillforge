"""add billing.calendar_alias

Revision ID: 0017_billing_calendar_alias
Revises: 0016_billing_payer
Create Date: 2026-11-11 00:00:00.000000

A name as written in the lesson calendar, stored normalised and mapped to a student (billing spec, SF4-2). The
student is referenced with RESTRICT (PD9).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017_billing_calendar_alias"
down_revision: str | None = "0016_billing_payer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calendar_alias",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name_normalized", sa.Text(), nullable=False),
        sa.Column("student_party_id", sa.UUID(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["student_party_id"], ["core.party.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name_normalized"),
        schema="billing",
    )
    op.create_index(
        "ix_calendar_alias_student_party_id", "calendar_alias", ["student_party_id"], unique=False, schema="billing"
    )


def downgrade() -> None:
    op.drop_index("ix_calendar_alias_student_party_id", table_name="calendar_alias", schema="billing")
    op.drop_table("calendar_alias", schema="billing")
