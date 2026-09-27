import uuid

from sqlalchemy import BigInteger, ForeignKey, Index, true
from sqlalchemy.orm import Mapped, mapped_column

from ..shared import TimestampMixin
from .base import ExtBase


class DiscordAccount(TimestampMixin, ExtBase):
    """A Discord link: which Discord user speaks for which person party.

    Identity, not CRM data (ADR 0009): ``app/services/auth/discord_links.py`` is its only writer. Like a user
    account (ADR 0008), it is no part of the party's ORM graph - ``party_id`` is the whole link.
    """

    __tablename__ = "discord_account"

    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    party_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.party.id", ondelete="CASCADE"),
        nullable=False,
    )

    is_primary: Mapped[bool] = mapped_column(nullable=False, default=False)
    active: Mapped[bool] = mapped_column(nullable=False, default=True)

    __table_args__ = ExtBase.extend_table_args(
        Index("ix_discord_account_party_id_active", "party_id", "active"),
        Index(
            "uq_discord_account_party_id_primary_active",
            "party_id",
            unique=True,
            postgresql_where=is_primary.is_(true()) & active.is_(true()),
        ),
    )
