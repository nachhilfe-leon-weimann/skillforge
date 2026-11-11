"""The calendar import: the calendar proposes, a person confirms (billing spec, "Calendar import").

``import_calendar`` reads a range of Berlin days and turns each occurrence into a lesson by the pure rules of
``calendar_rules.py``, or keeps it with its reason. ``billing.calendar_event`` holds every occurrence as last seen,
and the import compares the calendar with that: a ``planned`` lesson follows a moved, resized or cancelled
appointment and goes with a vanished one; a lesson that is no longer ``planned`` - ticked, cancelled, not held - is
never changed: its occurrence is flagged ``changed`` instead. Only an occurrence that starts inside the range can
vanish. An occurrence that has a lesson keeps its student and subject: a later alias or a renamed student does not
move it. ``dismiss`` takes an occurrence off the list (and acknowledges a difference); ``assign`` gives a waiting one
its lesson through ``lessons.py``.

The calendar is read before anything is written, so a failed read changes no row; every integration error becomes
``CalendarUnavailableError``. The writers of this table hold one transaction-level advisory lock, so overlapping
imports and assignments run one after the other. A title holds a name (PD13): ``title_name`` is kept only while an
occurrence waits for an assignment, the counts and log lines name nobody, and unassigned and dismissed occurrences
are deleted 90 Berlin days after their start, with the next import.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import ColumnElement, Exists, and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import CalendarAlias, CalendarEvent, Lesson, LessonStatus
from app.core.logging import get_logger
from app.integrations.errors import IntegrationError
from app.integrations.graph.client import GraphClient, Sleep
from app.integrations.graph.config import GraphSettings
from app.integrations.graph.credential import TOKENS, TokenCache
from app.integrations.graph.models import CalendarEvent as GraphEvent
from app.integrations.transport import InnerTransport
from app.services.crm.inputs import PartyRole
from app.services.crm.parties import list_parties

from . import calendar_aliases, lessons
from .calendar_rules import (
    LESSON_STEP,
    LessonProposal,
    NoLesson,
    NoLessonReason,
    Occurrence,
    ProposedStatus,
    Roster,
    StudentName,
    is_cancelled,
    parse_title,
    propose,
)
from .errors import (
    CalendarEventAlreadyAssignedError,
    CalendarEventNotFoundError,
    CalendarUnavailableError,
    LessonAlreadyExistsError,
)
from .lessons import BILLING_TIME_ZONE, berlin_midnight

type CalendarReader = Callable[[datetime, datetime], Awaitable[list[GraphEvent]]]
"""Every occurrence of the lesson calendar from one instant to another; raises an ``IntegrationError``."""

RETENTION_DAYS = 90
"""An occurrence without a lesson is deleted this many Berlin days after its start (PD13)."""
WAITING = frozenset({
    NoLessonReason.NO_TITLE_PATTERN,
    NoLessonReason.DURATION,
    NoLessonReason.UNMATCHED,
    NoLessonReason.AMBIGUOUS,
})
"""The reasons that leave an occurrence waiting for an assignment ("Shows as unassigned")."""

# One key for every writer of billing.calendar_event: the import, assign and dismiss.
_CALENDAR_LOCK = 0x5346_4341_4C45
_ROSTER_PAGE = 500

logger = get_logger(__name__)


@dataclass
class ImportCounts:
    """What an import did: counts only - no name, no title, no id."""

    occurrences: int = 0
    lessons_created: int = 0
    lessons_updated: int = 0
    lessons_removed: int = 0
    changed: int = 0
    unassigned: int = 0
    deleted: int = 0


@dataclass(frozen=True)
class ListedEvent:
    event: CalendarEvent
    lesson_id: uuid.UUID | None


def graph_calendar(
    settings: GraphSettings,
    *,
    transport: InnerTransport | None = None,
    tokens: TokenCache = TOKENS,
    sleep: Sleep = asyncio.sleep,
) -> CalendarReader:
    """The lesson calendar through Microsoft Graph: each read connects anew, so the calendar is confirmed every time."""

    async def read(start: datetime, end: datetime) -> list[GraphEvent]:
        async with GraphClient.connect(settings, transport=transport, tokens=tokens, sleep=sleep) as client:
            return await client.calendar_view(start, end)

    return read


async def import_calendar(
    session: AsyncSession, read: CalendarReader, *, first_day: date, last_day: date, now: datetime
) -> ImportCounts:
    """Import the occurrences of the Berlin days ``first_day`` to ``last_day``, both included; ``now`` stamps
    ``last_seen_at`` and decides PD13's retention."""
    start, end = berlin_midnight(first_day), berlin_midnight(last_day + timedelta(days=1))
    read_events = await _read(read, start, end)
    await _lock_calendar(session)

    returned = {event.id: event for event in read_events}
    stored = {
        event.external_id: event
        for event in await session.scalars(
            select(CalendarEvent).where(or_(CalendarEvent.external_id.in_(list(returned)), _starts_within(start, end)))
        )
    }
    linked = {
        lesson.calendar_event_id: lesson
        for lesson in await session.scalars(
            select(Lesson)
            .where(Lesson.calendar_event_id.in_([event.id for event in stored.values()]))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    roster = await _roster(session)
    counts = ImportCounts()

    # An occurrence that starts outside the range is another range's, unless it moved there from a stored one.
    for graph_event in sorted(returned.values(), key=lambda item: (item.start, item.id)):
        event = stored.get(graph_event.id)
        if event is None and not start <= graph_event.start < end:
            continue
        if event is None:
            event = CalendarEvent(id=uuid.uuid4(), external_id=graph_event.id, dismissed=False, changed=False)
            session.add(event)
        await _take(session, event, linked.get(event.id), _occurrence(graph_event), roster, counts, now=now)
        counts.occurrences += 1

    for event in stored.values():
        if event.external_id not in returned and start <= event.starts_at < end:
            await _vanished(session, event, linked.get(event.id), counts)
    await session.flush()

    counts.deleted += await _expire(session, now)
    counts.changed, counts.unassigned = (
        await session.execute(
            select(
                func.count().filter(CalendarEvent.changed),
                func.count().filter(_waiting()),
            ).where(_starts_within(start, end))
        )
    ).one()
    logger.info("calendar_imported", **asdict(counts))
    return counts


async def list_events(session: AsyncSession, *, limit: int, offset: int) -> tuple[list[ListedEvent], int]:
    """Return one page of the occurrences that wait for an assignment or differ from their lesson, by start."""
    listed = or_(CalendarEvent.changed, _waiting())
    total = await session.scalar(select(func.count()).select_from(CalendarEvent).where(listed))
    rows = await session.execute(
        select(CalendarEvent, Lesson.id)
        .outerjoin(Lesson, Lesson.calendar_event_id == CalendarEvent.id)
        .where(listed)
        .order_by(CalendarEvent.starts_at, CalendarEvent.id)
        .limit(limit)
        .offset(offset)
    )
    return [ListedEvent(event, lesson_id) for event, lesson_id in rows], total or 0


async def assign_event(
    session: AsyncSession,
    event_id: uuid.UUID,
    *,
    student_id: uuid.UUID,
    subject: str,
    minutes: int,
    alias: str | None = None,
) -> Lesson:
    """Give an occurrence without a lesson its lesson, through the lesson services: a ``cancelled`` one for a cancelled
    occurrence. ``alias`` is stored for the student first, as ``POST /calendar/aliases`` would."""
    await _lock_calendar(session)
    event = await _get_event(session, event_id)
    if await session.scalar(select(exists().where(Lesson.calendar_event_id == event.id))):
        raise CalendarEventAlreadyAssignedError(f"Calendar event {event_id} has a lesson")
    if alias is not None:
        await calendar_aliases.create_alias(session, name=alias, student_id=student_id)

    lesson = await lessons.create_lesson(
        session,
        student_id=student_id,
        subject=subject,
        starts_at=event.starts_at,
        minutes=minutes,
        calendar_event_id=event.id,
    )
    if event.cancelled:
        lesson = await lessons.update_lesson(session, lesson.id, status=LessonStatus.CANCELLED)
    event.title_name, event.reason, event.changed, event.dismissed = None, None, False, False
    await session.flush()
    return lesson


async def dismiss_event(session: AsyncSession, event_id: uuid.UUID) -> None:
    """Take the occurrence off the list: it waits for nothing, and a difference from its lesson is acknowledged.
    Idempotent. The import proposes a dismissed occurrence without a lesson no more."""
    await _lock_calendar(session)
    event = await _get_event(session, event_id)
    event.dismissed, event.changed, event.title_name = True, False, None
    await session.flush()


async def _read(read: CalendarReader, start: datetime, end: datetime) -> list[GraphEvent]:
    try:
        return await read(start, end)
    except IntegrationError as exc:
        # No chaining and no text of the cause: it may carry the remote system's message.
        logger.warning("calendar_unavailable", error=type(exc).__name__, status=exc.status)
        raise CalendarUnavailableError(f"The calendar read failed ({type(exc).__name__})") from None


async def _take(
    session: AsyncSession,
    event: CalendarEvent,
    lesson: Lesson | None,
    occurrence: Occurrence,
    roster: Roster,
    counts: ImportCounts,
    *,
    now: datetime,
) -> None:
    """Store a returned occurrence as seen, then bring its lesson up to date - or propose one."""
    cancelled = is_cancelled(occurrence)
    seen_before = (event.starts_at, event.ends_at, event.cancelled)
    title = parse_title(occurrence.title)
    event.starts_at, event.ends_at, event.cancelled = occurrence.starts_at, occurrence.ends_at, cancelled
    event.title_subject = title.subject if title is not None else None
    event.last_seen_at = now

    if lesson is not None:
        # Seen again, so a vanishing the maintainer acknowledged is over.
        event.reason, event.title_name, event.dismissed = None, None, False
        if seen_before != (event.starts_at, event.ends_at, event.cancelled):
            await _follow(session, event, lesson, occurrence, cancelled, counts)
    elif event.dismissed:
        event.title_name = None
    else:
        await _propose(session, event, occurrence, roster, counts)


async def _follow(
    session: AsyncSession,
    event: CalendarEvent,
    lesson: Lesson,
    occurrence: Occurrence,
    cancelled: bool,
    counts: ImportCounts,
) -> None:
    """The appointment changed: a ``planned`` lesson follows it, any other is flagged. So is a ``planned`` one that
    cannot follow - a duration off the quarter hour, or another lesson of the student at the new start."""
    minutes = _minutes(occurrence)
    if lesson.status is not LessonStatus.PLANNED or minutes is None:
        event.changed = True
        return

    # A planned lesson becomes cancelled with its appointment; one cancelled no more stays planned.
    status = LessonStatus.CANCELLED if cancelled else None
    if (lesson.starts_at, lesson.minutes, status) == (occurrence.starts_at, minutes, None):
        return
    try:
        await lessons.update_lesson(session, lesson.id, starts_at=occurrence.starts_at, minutes=minutes, status=status)
    except LessonAlreadyExistsError:
        event.changed = True
        return
    counts.lessons_updated += 1


async def _propose(
    session: AsyncSession, event: CalendarEvent, occurrence: Occurrence, roster: Roster, counts: ImportCounts
) -> None:
    """Run the rules for an occurrence without a lesson; a lesson they propose is created through the lesson services.

    The name is kept only for the reasons that wait (``NoLesson.title``). A student who has a lesson at that start
    already - added by hand - gets no second one: the occurrence is flagged ``changed`` for the maintainer.
    """
    match propose(occurrence, roster):
        case NoLesson(reason=reason, title=title):
            event.reason, event.changed = reason, False
            event.title_name = title.name if title is not None else None
        case LessonProposal() as proposal:
            event.reason, event.title_name = None, None
            created = await _create(session, event, proposal, occurrence.starts_at)
            event.changed = not created
            if created:
                counts.lessons_created += 1


async def _create(session: AsyncSession, event: CalendarEvent, proposal: LessonProposal, starts_at: datetime) -> bool:
    try:
        lesson = await lessons.create_lesson(
            session,
            student_id=proposal.student_id,
            subject=proposal.subject,
            starts_at=starts_at,
            minutes=proposal.minutes,
            calendar_event_id=event.id,
        )
    except LessonAlreadyExistsError:
        return False
    if proposal.status is ProposedStatus.CANCELLED:
        await lessons.update_lesson(session, lesson.id, status=LessonStatus.CANCELLED)
    return True


async def _vanished(session: AsyncSession, event: CalendarEvent, lesson: Lesson | None, counts: ImportCounts) -> None:
    """The occurrence is gone from the range: so is a ``planned`` lesson with it, and an occurrence without one; any
    other lesson stays and its occurrence is flagged - unless the maintainer acknowledged that already."""
    if lesson is not None and lesson.status is not LessonStatus.PLANNED:
        if not event.dismissed:
            event.changed = True
        return

    if lesson is not None:
        await session.delete(lesson)
        await session.flush()
        counts.lessons_removed += 1
    await session.delete(event)
    counts.deleted += 1


async def _expire(session: AsyncSession, now: datetime) -> int:
    """Delete every occurrence without a lesson that started ``RETENTION_DAYS`` Berlin days ago or earlier (PD13)."""
    today = now.astimezone(BILLING_TIME_ZONE).date()
    cutoff = berlin_midnight(today - timedelta(days=RETENTION_DAYS - 1))
    expired = await session.scalars(
        delete(CalendarEvent)
        .where(CalendarEvent.starts_at < cutoff, ~_has_lesson())
        .returning(CalendarEvent.id)
        .execution_options(synchronize_session=False)
    )
    return len(expired.all())


async def _roster(session: AsyncSession) -> Roster:
    """The students with their names, read through the CRM, and the aliases."""
    students: list[StudentName] = []
    offset = 0
    while True:
        page, total = await list_parties(session, limit=_ROSTER_PAGE, offset=offset, role=PartyRole.STUDENT)
        students += [
            StudentName(party.id, party.person.firstname, party.person.lastname)
            for party in page
            if party.person is not None
        ]
        offset += len(page)
        if not page or offset >= total:
            break
    aliases = await session.execute(select(CalendarAlias.name_normalized, CalendarAlias.student_party_id))
    return Roster(students=tuple(students), aliases=dict(aliases.all()))


async def _lock_calendar(session: AsyncSession) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(_CALENDAR_LOCK)))


async def _get_event(session: AsyncSession, event_id: uuid.UUID) -> CalendarEvent:
    event = await session.get(CalendarEvent, event_id, populate_existing=True)
    if event is None:
        raise CalendarEventNotFoundError(f"No calendar event with id {event_id}")
    return event


def _occurrence(event: GraphEvent) -> Occurrence:
    return Occurrence(
        title=event.subject,
        starts_at=event.start,
        ends_at=event.end,
        all_day=event.is_all_day,
        cancelled=event.is_cancelled,
        categories=event.categories,
        location=event.location,
        online_meeting=event.is_online_meeting,
    )


def _minutes(occurrence: Occurrence) -> int | None:
    """The duration in minutes, if a lesson can have it: more than nothing, at most a day and on the quarter hour
    (rule G) - the bounds ``propose`` applies to a new lesson."""
    duration = occurrence.ends_at - occurrence.starts_at
    if duration <= timedelta() or duration > timedelta(days=1) or duration % LESSON_STEP:
        return None
    return duration // timedelta(minutes=1)


def _starts_within(start: datetime, end: datetime) -> ColumnElement[bool]:
    return and_(CalendarEvent.starts_at >= start, CalendarEvent.starts_at < end)


def _waiting() -> ColumnElement[bool]:
    """An occurrence that waits for an assignment: no lesson, not dismissed, and a reason that shows as unassigned."""
    return and_(
        ~CalendarEvent.dismissed,
        CalendarEvent.reason.in_(WAITING),
        ~_has_lesson(),
    )


def _has_lesson() -> Exists:
    # Correlated to the occurrence only: the list joins the lesson too.
    return exists().where(Lesson.calendar_event_id == CalendarEvent.id).correlate_except(Lesson)
