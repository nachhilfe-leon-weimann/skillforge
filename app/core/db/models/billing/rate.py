import uuid
from datetime import date

from sqlalchemy import UUID, CheckConstraint, Date, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..shared import TimestampMixin
from .base import BillingBase


class Rate(TimestampMixin, BillingBase):
    """A student's own rate from ``valid_from`` on, in cents per hour (billing spec, decision E).

    The rate belongs to the student party, not to a subject. Rule G holds in the database: cents per hour are
    divisible by 4, so every quarter hour is a whole cent. ``student_party_id`` is ``RESTRICT``: the CRM's delete
    guard names a party with a rate before the database refuses it (PD9).
    """

    __tablename__ = "rate"
    __table_args__ = BillingBase.extend_table_args(
        UniqueConstraint("student_party_id", "valid_from", name="uq_rate_student_valid_from"),
        CheckConstraint("cents_per_hour >= 0 AND cents_per_hour % 4 = 0", name="ck_rate_quarter_hour_cents"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    student_party_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.party.id", ondelete="RESTRICT"), nullable=False
    )

    cents_per_hour: Mapped[int] = mapped_column(nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
