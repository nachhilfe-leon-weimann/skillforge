"""Lessons: one appointment of one student, from ``planned`` to ticked (billing spec, "A lesson's life")."""

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import Lesson, LessonStatus
from app.core.unset import UNSET, Unset
from app.services.crm.errors import PartyNotFoundError
from app.services.crm.parties import load_party

from .errors import (
    LessonAlreadyExistsError,
    LessonNotBillableError,
    LessonNotFoundError,
    RateNotQuarterHourError,
    UnknownStudentError,
)

# Lessons are stored in UTC and billed by the Berlin calendar, daylight saving included (money rule 9).
BILLING_TIME_ZONE = ZoneInfo("Europe/Berlin")

# Only these statuses admit `billable` (ck_lesson_billable_status); of them only the tick makes a lesson billable by
# itself - a late cancellation becomes billable only when the maintainer says so (decision M).
BILLABLE_STATUSES = frozenset({LessonStatus.HELD, LessonStatus.CANCELLED})

_UNIQUE_START = "uq_lesson_student_start"


async def list_lessons(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    from_day: date | None = None,
    to_day: date | None = None,
    student_id: uuid.UUID | None = None,
    status: LessonStatus | None = None,
) -> tuple[list[Lesson], int]:
    """Return one page of lessons by start, plus the size of the filtered set.

    ``from_day`` and ``to_day`` are Berlin days, both included. Filters never fail: a combination nothing can match
    (a reversed range, a party without lessons) is an empty page.
    """
    filters: list[ColumnElement[bool]] = []
    if from_day is not None:
        filters.append(Lesson.starts_at >= berlin_midnight(from_day))
    if to_day is not None:
        filters.append(Lesson.starts_at < berlin_midnight(to_day + timedelta(days=1)))
    if student_id is not None:
        filters.append(Lesson.student_party_id == student_id)
    if status is not None:
        filters.append(Lesson.status == status)

    total = await session.scalar(select(func.count()).select_from(Lesson).where(*filters))
    lessons = await session.scalars(
        select(Lesson).where(*filters).order_by(Lesson.starts_at, Lesson.id).limit(limit).offset(offset)
    )
    return list(lessons), total or 0


async def get_lesson(session: AsyncSession, lesson_id: uuid.UUID) -> Lesson:
    lesson = await session.get(Lesson, lesson_id)
    if lesson is None:
        raise LessonNotFoundError(f"No lesson with id {lesson_id}")

    return lesson


async def create_lesson(
    session: AsyncSession,
    *,
    student_id: uuid.UUID,
    subject: str,
    starts_at: datetime,
    minutes: int,
    rate_cents_per_hour: int | None = None,
    note: str | None = None,
) -> Lesson:
    """Add a lesson by hand. It starts ``planned`` and not billable: the tick is a change of its status."""
    await _require_student(session, student_id)
    _require_quarter_hour_rate(rate_cents_per_hour)

    lesson = Lesson(
        student_party_id=student_id,
        subject=subject,
        starts_at=starts_at,
        minutes=minutes,
        rate_cents_per_hour=rate_cents_per_hour,
        note=note,
    )
    async with _unique_start(session):
        session.add(lesson)

    await session.refresh(lesson)
    return lesson


async def update_lesson(
    session: AsyncSession,
    lesson_id: uuid.UUID,
    *,
    status: LessonStatus | None = None,
    billable: bool | None = None,
    student_id: uuid.UUID | None = None,
    subject: str | None = None,
    starts_at: datetime | None = None,
    minutes: int | None = None,
    rate_cents_per_hour: int | None | Unset = UNSET,
    note: str | None | Unset = UNSET,
) -> Lesson:
    """Change the given fields. ``None`` means unchanged for the ``NOT NULL`` columns, ``UNSET`` for the others,
    whose ``None`` clears them. A field sent with its current value changes nothing, and neither does an empty body.

    The lesson is locked first: two overlapping changes of one lesson - a tick and a move to ``planned`` - must not
    combine into a row ``ck_lesson_billable_status`` refuses.
    """
    lesson = await _get_lesson_for_update(session, lesson_id)
    if student_id is not None and student_id != lesson.student_party_id:
        await _require_student(session, student_id)

    sent: dict[str, Any] = {
        name: value
        for name, value in (
            ("status", status),
            ("billable", billable_after(lesson, status=status, billable=billable)),
            ("student_party_id", student_id),
            ("subject", subject),
            ("starts_at", starts_at),
            ("minutes", minutes),
        )
        if value is not None
    }
    if rate_cents_per_hour is not UNSET:
        _require_quarter_hour_rate(rate_cents_per_hour)
        sent["rate_cents_per_hour"] = rate_cents_per_hour
    if note is not UNSET:
        sent["note"] = note

    changes = {name: value for name, value in sent.items() if getattr(lesson, name) != value}
    if not changes:
        return lesson

    async with _unique_start(session):
        for name, value in changes.items():
            setattr(lesson, name, value)

    await session.refresh(lesson)
    return lesson


def billable_after(lesson: Lesson, *, status: LessonStatus | None, billable: bool | None) -> bool:
    """Return the lesson's ``billable`` after a change of ``status`` and ``billable`` (``None``: not sent).

    Entering a status sets its default: the tick (``held``) makes a lesson billable, every other status does not, so
    a billable lesson moved to ``planned`` or ``not_held`` - or to ``cancelled`` - loses the flag. Sending the status
    the lesson already has enters nothing: a ``held`` lesson whose flag was switched off (goodwill) stays off. A
    ``billable`` sent along wins over the default, and ``true`` needs a status of ``BILLABLE_STATUSES``.
    """
    new_status = lesson.status if status is None else status
    if billable is None:
        return lesson.billable if new_status == lesson.status else new_status == LessonStatus.HELD
    if billable and new_status not in BILLABLE_STATUSES:
        raise LessonNotBillableError(f"A {new_status} lesson cannot be billable")

    return billable


def berlin_midnight(day: date) -> datetime:
    """The instant ``day`` begins in Berlin. Midnight exists on every day: the clocks change at 2 and 3 a.m."""
    return datetime.combine(day, time(), tzinfo=BILLING_TIME_ZONE)


async def _get_lesson_for_update(session: AsyncSession, lesson_id: uuid.UUID) -> Lesson:
    lesson = await session.scalar(
        select(Lesson).where(Lesson.id == lesson_id).with_for_update().execution_options(populate_existing=True)
    )
    if lesson is None:
        raise LessonNotFoundError(f"No lesson with id {lesson_id}")

    return lesson


async def _require_student(session: AsyncSession, party_id: uuid.UUID) -> None:
    """Raise ``UnknownStudentError`` unless the party holds the student role, read through the CRM's loading path."""
    try:
        party = await load_party(session, party_id)
    except PartyNotFoundError:
        raise UnknownStudentError(f"No party with id {party_id}") from None
    if party.person is None or party.person.student is None:
        raise UnknownStudentError(f"Party {party_id} holds no student role")


def _require_quarter_hour_rate(cents_per_hour: int | None) -> None:
    """Rule G for a lesson's own rate (PD3): checked here, so ``ck_lesson_quarter_hour_cents`` never answers."""
    if cents_per_hour is not None and (cents_per_hour < 0 or cents_per_hour % 4):
        raise RateNotQuarterHourError(f"{cents_per_hour} cents per hour is not a quarter-hour rate")


@asynccontextmanager
async def _unique_start(session: AsyncSession) -> AsyncGenerator[None]:
    """Write the change made inside the block in a SAVEPOINT and translate a ``uq_lesson_student_start`` conflict.

    Uniqueness is decided by the constraint, not by check-then-insert, so two overlapping requests cannot both pass:
    the second waits on the index and then fails here, not as a 500. Any other violation is raised as it is. The
    change must happen *inside* the block: ``begin_nested()`` first flushes whatever is pending into the enclosing
    transaction, where a violation would take it down.
    """
    try:
        async with session.begin_nested():
            yield
            await session.flush()
    except IntegrityError as exc:
        if _violated_constraint(exc) != _UNIQUE_START:
            raise
        raise LessonAlreadyExistsError(f"{_UNIQUE_START} violated") from exc


def _violated_constraint(exc: IntegrityError) -> str | None:
    """The name of the constraint ``exc`` violated; asyncpg's own error, the cause of the DBAPI one, has it."""
    return getattr(exc.orig.__cause__, "constraint_name", None) if exc.orig is not None else None
