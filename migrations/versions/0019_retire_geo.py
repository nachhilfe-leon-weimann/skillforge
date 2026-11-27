"""retire the geo schema

Revision ID: 0019_retire_geo
Revises: 0018_billing_calendar_event
Create Date: 2026-11-27 00:00:00.000000

``geo.plz_ort`` held postal-code reference data from a one-off import; no code reads or writes it and no loader
exists. A frozen snapshot: this revision imports no app code, so it keeps working without the geo models.

Upgrade: the table, then the schema. ``DROP SCHEMA`` runs without ``CASCADE``: an object this revision does not know
fails the upgrade, and the whole migration rolls back.

Downgrade: the empty structure of 0001_baseline - columns and primary key under their original names. No data:
what the upgrade deleted comes back only from a backup.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019_retire_geo"
down_revision: str | None = "0018_billing_calendar_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Queue at most 15 s behind a running transaction - and never make every later query queue behind this one.
    op.execute(sa.text("SET LOCAL lock_timeout = '15s'"))
    op.drop_table("plz_ort", schema="geo")
    # No CASCADE: an object this revision does not know fails here, and the whole migration rolls back.
    op.execute(sa.text("DROP SCHEMA geo"))


def downgrade() -> None:
    op.execute(sa.text("CREATE SCHEMA geo"))
    op.create_table(
        "plz_ort",
        sa.Column("plz", sa.String(length=5), nullable=False),
        sa.Column("ort", sa.String(), nullable=False),
        sa.Column("landkreis", sa.String(), nullable=False),
        sa.Column("bundesland", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("plz", "ort"),
        schema="geo",
    )
