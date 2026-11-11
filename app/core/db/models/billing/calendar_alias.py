import uuid

from sqlalchemy import UUID, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..shared import TimestampMixin
from .base import BillingBase


class CalendarAlias(TimestampMixin, BillingBase):
    """A name as written in the lesson calendar, mapped to a student (billing spec, "Calendar import").

    The name is stored normalised, as the import compares it, so one name maps to one student. An alias names a
    person: it is the maintainer's mapping and appears in no log line. ``student_party_id`` is ``RESTRICT``: the
    CRM's delete guard names a party with an alias before the database refuses it (PD9).
    """

    __tablename__ = "calendar_alias"
    __table_args__ = BillingBase.extend_table_args(
        # The delete guard asks for a party's aliases.
        Index("ix_calendar_alias_student_party_id", "student_party_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    name_normalized: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    student_party_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.party.id", ondelete="RESTRICT"), nullable=False
    )
