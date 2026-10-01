import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import UUID, DateTime, Enum, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..shared import CreatedAtMixin
from .base import AuthBase

if TYPE_CHECKING:
    from .user_account import UserAccount


class UserActionTokenPurpose(enum.StrEnum):
    INVITATION = "invitation"
    PASSWORD_RESET = "password_reset"
    DISCORD_LINK = "discord_link"


class UserActionToken(CreatedAtMixin, AuthBase):
    """A one-time token: an invitation, a password reset or a Discord link code. Stored as a SHA-256 hash
    only; the plaintext (prefix ``sf_ua_``) exists once, in the response that issues it."""

    __tablename__ = "user_action_token"
    __table_args__ = AuthBase.extend_table_args(
        Index("ix_user_action_token_user_account_id", "user_account_id"),
        Index("ix_user_action_token_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("auth.user_account.id", ondelete="CASCADE"), nullable=False
    )

    purpose: Mapped[UserActionTokenPurpose] = mapped_column(
        Enum(
            UserActionTokenPurpose,
            name="user_action_token_purpose",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
    )

    token_hash: Mapped[str] = mapped_column(nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    issued_by: Mapped[str] = mapped_column(nullable=False)

    user_account: Mapped[UserAccount] = relationship("UserAccount", back_populates="action_tokens")
