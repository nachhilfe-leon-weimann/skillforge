import uuid

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..shared import TimestampMixin
from .base import BillingBase


class ManualPayer(TimestampMixin, BillingBase):
    """A payer billed by hand (billing spec, PD5): their lessons show as "bill manually" and never reach a draft.

    The party is the key - a payer is marked or not - so the table has no ``id`` of its own. ``RESTRICT``: the CRM's
    delete guard names a marked party before the database refuses it (PD9).
    """

    __tablename__ = "manual_payer"

    party_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.party.id", ondelete="RESTRICT"), primary_key=True)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
