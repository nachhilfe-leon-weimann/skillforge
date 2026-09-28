"""add the discord_link purpose of one-time tokens

Revision ID: 0014_link_code_purpose
Revises: 0013_retire_bot
Create Date: 2026-09-27 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_link_code_purpose"
down_revision: str | None = "0013_retire_bot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Postgres 12+ allows ALTER TYPE ... ADD VALUE inside the migration's transaction as long as nothing in that
    # transaction uses the new label; nothing here does.
    op.execute(sa.text("ALTER TYPE public.user_action_token_purpose ADD VALUE IF NOT EXISTS 'discord_link'"))


def downgrade() -> None:
    # Postgres cannot drop an enum label, so recreate the type without it (the rename-aside pattern of 0007 and
    # 0009). The link codes go first - a row still holding the label would fail the cast - and they are one-time
    # tokens an admin can issue again. The column has no server default and no index predicated on the label,
    # so nothing else moves aside.
    op.execute(sa.text("DELETE FROM auth.user_action_token WHERE purpose = 'discord_link'"))
    op.execute(sa.text("ALTER TYPE public.user_action_token_purpose RENAME TO user_action_token_purpose_old"))
    op.execute(sa.text("CREATE TYPE public.user_action_token_purpose AS ENUM ('invitation', 'password_reset')"))
    op.execute(
        sa.text(
            "ALTER TABLE auth.user_action_token ALTER COLUMN purpose TYPE public.user_action_token_purpose "
            "USING purpose::text::public.user_action_token_purpose"
        )
    )
    op.execute(sa.text("DROP TYPE public.user_action_token_purpose_old"))
