"""index expires_at of sessions and one-time tokens for the housekeeping worker

Revision ID: 0012_auth_expiry_indexes
Revises: 0011_grant_mode_user_accounts
Create Date: 2026-09-25 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012_auth_expiry_indexes"
down_revision: str | None = "0011_grant_mode_user_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The housekeeping worker deletes by expires_at every cycle; without these its idle check scans the tables.
    op.create_index("ix_user_session_expires_at", "user_session", ["expires_at"], unique=False, schema="auth")
    op.create_index("ix_user_action_token_expires_at", "user_action_token", ["expires_at"], unique=False, schema="auth")


def downgrade() -> None:
    op.drop_index("ix_user_action_token_expires_at", table_name="user_action_token", schema="auth")
    op.drop_index("ix_user_session_expires_at", table_name="user_session", schema="auth")
