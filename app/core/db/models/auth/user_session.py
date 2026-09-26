import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import UUID, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..shared import CreatedAtMixin
from .base import AuthBase

if TYPE_CHECKING:
    from .application_client import ApplicationClient
    from .user_account import UserAccount


class UserSession(CreatedAtMixin, AuthBase):
    """One row per login of a person through a client (user-authentication spec, decision L). ``id``
    is the token's ``sid``; only the ``application_client`` that opened the session may refresh or
    revoke it. ``scope`` is the ceiling of every refresh and never rewritten. Refresh tokens are
    opaque, rotating and stored as SHA-256 hashes only - the plaintext exists once, in the response
    that creates them."""

    __tablename__ = "user_session"
    __table_args__ = AuthBase.extend_table_args(
        Index("ix_user_session_user_account_id", "user_account_id"),
        Index("ix_user_session_application_client_id", "application_client_id"),
        Index("ix_user_session_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("auth.user_account.id", ondelete="CASCADE"), nullable=False
    )
    application_client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("auth.application_client.id", ondelete="CASCADE"), nullable=False
    )

    scope: Mapped[str] = mapped_column(nullable=False)

    refresh_token_hash: Mapped[str] = mapped_column(nullable=False, unique=True)
    previous_refresh_token_hash: Mapped[str | None] = mapped_column(nullable=True, unique=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(nullable=True)

    user_account: Mapped[UserAccount] = relationship("UserAccount", back_populates="sessions")
    application_client: Mapped[ApplicationClient] = relationship("ApplicationClient")
