import uuid
from datetime import datetime

from sqlalchemy import UUID, CheckConstraint, DateTime, Index, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from ..shared import TimestampMixin
from .base import BillingBase


class CalendarEvent(TimestampMixin, BillingBase):
    """One occurrence of the lesson calendar as the import last saw it (billing spec, "Calendar import").

    A lesson points to its occurrence (``Lesson.calendar_event_id``); an occurrence without one has a ``reason``, is
    dismissed, or - ``changed`` - could not get its lesson. ``title_name`` is a person's name: it is kept only while
    the occurrence waits for an assignment (PD13), which ``ck_calendar_event_title_name`` holds for the reasons that
    wait, and it appears in no log line.
    """

    __tablename__ = "calendar_event"
    __table_args__ = BillingBase.extend_table_args(
        # The values of `NoLessonReason` in `calendar_rules.py`.
        CheckConstraint(
            "reason IN ('all_day', 'phone', 'first_meeting', 'no_title_pattern', 'duration', 'unmatched', 'ambiguous')",
            name="ck_calendar_event_reason",
        ),
        CheckConstraint(
            "title_name IS NULL"
            " OR (NOT dismissed AND reason IS NOT NULL AND reason IN ('duration', 'unmatched', 'ambiguous'))",
            name="ck_calendar_event_title_name",
        ),
        # The import asks for the occurrences of a range, and PD13's retention for the old ones.
        Index("ix_calendar_event_starts_at", "starts_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    external_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    """Graph's event ``id``: an occurrence keeps it when it moves (Q16)."""
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    title_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The text before `` mit ``."""
    title_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The text after `` mit ``: a name, kept only while the occurrence waits for an assignment (PD13)."""
    cancelled: Mapped[bool] = mapped_column(nullable=False)
    """Cancelled in the calendar or by the late-cancellation category (``is_cancelled``)."""
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Why no lesson was proposed (``NoLessonReason``); ``None`` once it has a lesson."""
    dismissed: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=false())
    changed: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=false())
    """The appointment differs from a lesson the import must not change, or it could not get its lesson."""
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """When an import last read the occurrence from the calendar."""
