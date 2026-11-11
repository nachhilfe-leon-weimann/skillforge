import enum
import uuid
from datetime import datetime

from sqlalchemy import UUID, CheckConstraint, DateTime, Enum, ForeignKey, Index, Text, UniqueConstraint, false, text
from sqlalchemy.orm import Mapped, mapped_column

from ..shared import TimestampMixin
from .base import BillingBase


class LessonStatus(enum.StrEnum):
    PLANNED = "planned"
    """Known, not confirmed: imported from the calendar or added by hand."""
    HELD = "held"
    """Ticked: it took place."""
    CANCELLED = "cancelled"
    """Cancelled late or not attended; the appointment stayed in the calendar."""
    NOT_HELD = "not_held"
    """Did not take place and costs nothing."""


class Lesson(TimestampMixin, BillingBase):
    """One appointment of one student (billing spec, "A lesson's life").

    Rule G holds in the database: minutes are divisible by 15 and the lesson's own rate (PD3), if any, by 4 - so
    every amount is a whole cent. ``billable`` is only ever true for a ``held`` or ``cancelled`` lesson.
    ``student_party_id`` and ``payer_party_id`` are ``RESTRICT``: the CRM's delete guard names a party with a lesson
    before the database refuses it (PD9).
    """

    __tablename__ = "lesson"
    __table_args__ = BillingBase.extend_table_args(
        UniqueConstraint("student_party_id", "starts_at", name="uq_lesson_student_start"),
        CheckConstraint("minutes > 0 AND minutes % 15 = 0", name="ck_lesson_quarter_hour_minutes"),
        CheckConstraint(
            "rate_cents_per_hour >= 0 AND rate_cents_per_hour % 4 = 0", name="ck_lesson_quarter_hour_cents"
        ),
        CheckConstraint("NOT billable OR status IN ('held', 'cancelled')", name="ck_lesson_billable_status"),
        # The delete guard asks for a party's lessons as payer, as it does for the student (uq_lesson_student_start).
        Index("ix_lesson_payer_party_id", "payer_party_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    student_party_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.party.id", ondelete="RESTRICT"), nullable=False
    )

    subject: Mapped[str] = mapped_column(Text, nullable=False)
    """The label of the calendar title (``Mathe``), not a ``core.subject`` (PD4)."""
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    minutes: Mapped[int] = mapped_column(nullable=False)

    status: Mapped[LessonStatus] = mapped_column(
        Enum(
            LessonStatus,
            name="lesson_status",
            schema="billing",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
        default=LessonStatus.PLANNED,
        server_default=text("'planned'"),
    )
    billable: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=false())

    payer_party_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.party.id", ondelete="RESTRICT"), nullable=True
    )
    """Who pays, stored when the lesson becomes billable (PD1): the CRM's payer then, kept when ``pays_for`` changes
    later. ``None`` while the lesson is not billable, and for a billable one whose student had several payers."""

    calendar_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("billing.calendar_event.id", ondelete="RESTRICT"), nullable=True, unique=True
    )
    """The occurrence the lesson comes from; ``None`` for a lesson added by hand. ``RESTRICT``: only the import deletes
    an occurrence with its lesson, and the lesson first."""

    rate_cents_per_hour: Mapped[int | None] = mapped_column(nullable=True)
    """The lesson's own rate (PD3); ``None`` resolves the student's rate or the default."""
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
