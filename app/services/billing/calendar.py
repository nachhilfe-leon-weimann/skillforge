"""The calendar import: the calendar proposes, a person confirms (billing spec, "Calendar import").

``import_calendar`` reads a range of Berlin days and turns each occurrence into a lesson by the pure rules of
``calendar_rules.py``, or keeps it with its reason. ``billing.calendar_event`` holds every occurrence as last seen,
and the import compares the calendar with that. When in doubt it flags ``changed`` and changes nothing: it never
invents, moves, duplicates or drops a lesson a person may have looked at.

- An occurrence with a lesson is judged by the rules again at every import. Only while they still give this student
  and this subject, and the lesson is untouched - ``planned``, no note, no own rate, at the start and length of its
  occurrence as last seen - does it follow a moved, resized or cancelled appointment. An untouched lesson goes
  with a vanished appointment, whatever its title said.
- Flagged: the title names another student or subject - by the rules' answer, or by its name where the rules stop
  before it (a subject only in a lesson title) - at every import, until the lesson, the title or an alias settles
  it; the rules give no lesson; a touched lesson's appointment changed or vanished; a lesson that cannot follow.
  ``dismiss`` - and an assignment - acknowledges the occurrence as last seen: a "no lesson" answer and a change
  already seen stay quiet until start, end, cancellation, subject, a new no-lesson reason or presence changes.
  Another student or subject is never acknowledged; a name that matches nobody or several cannot be compared (PD13
  keeps no name). A vanishing is flagged once (``vanished``).
- An occurrence without a lesson is proposed at every import until it gets one, unless it was dismissed or flagged
  because it could not get its lesson: then it waits for the maintainer. ``assign`` gives it its lesson through
  ``lessons.py``. Only an occurrence that starts inside the range can vanish.

The calendar is read first - outside any lock and before any statement, so a failed read changes no row and the
Graph client's time limit covers the read alone; every integration error becomes ``CalendarUnavailableError``. The
writers of this table hold one transaction-level advisory lock, so overlapping imports and assignments run one after
the other. A title holds a name (PD13): ``title_name`` is kept only while an occurrence waits for an assignment, the
counts and log lines name nobody, and occurrences without a lesson are deleted 90 Berlin days after their start,
with the next import.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import ColumnElement, Exists, and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import CalendarAlias, CalendarEvent, Lesson, LessonStatus
from app.core.logging import get_logger
from app.integrations.errors import IntegrationError
from app.integrations.graph.client import Clock, GraphClient, Sleep
from app.integrations.graph.config import GraphSettings
from app.integrations.graph.credential import TOKENS, TokenCache
from app.integrations.graph.models import CalendarEvent as GraphEvent
from app.integrations.transport import InnerTransport
from app.services.crm.inputs import PartyRole
from app.services.crm.parties import list_parties

from . import calendar_aliases, lessons
from .calendar_rules import (
    LessonProposal,
    NoLesson,
    NoLessonReason,
    Occurrence,
    Proposal,
    ProposedStatus,
    Roster,
    StudentName,
    is_cancelled,
    is_on_the_minute,
    match_name,
    parse_title,
    propose,
)
from .errors import (
    CalendarEventAlreadyAssignedError,
    CalendarEventNotAssignableError,
    CalendarEventNotFoundError,
    CalendarUnavailableError,
    LessonAlreadyExistsError,
    UnknownStudentError,
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
    clock: Clock = time.monotonic,
) -> CalendarReader:
    """The lesson calendar through Microsoft Graph: each read connects anew, so the calendar is confirmed every time.
    The block holds the read alone - the client's deadline covers it - and hands over the whole listing or raises."""

    async def read(start: datetime, end: datetime) -> list[GraphEvent]:
        async with GraphClient.connect(
            settings, transport=transport, tokens=tokens, sleep=sleep, clock=clock
        ) as client:
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
            event = CalendarEvent(
                id=uuid.uuid4(), external_id=graph_event.id, dismissed=False, changed=False, vanished=False
            )
            session.add(event)
        await _take(session, event, linked.get(event.id), _occurrence(graph_event), roster, counts, now=now)
        counts.occurrences += 1

    # Of the stored occurrences only those that start in the range can be missing from the read (`stored`'s query).
    for event in stored.values():
        if event.external_id not in returned:
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
    occurrence. ``alias`` is stored for the student first, as ``POST /calendar/aliases`` would.

    The lesson starts at the occurrence's start, so a start off the minute is refused before anything is written; the
    end does not count, the request's ``minutes`` give the length."""
    await _lock_calendar(session)
    event = await _get_event(session, event_id)
    if await session.scalar(select(exists().where(Lesson.calendar_event_id == event.id))):
        raise CalendarEventAlreadyAssignedError(f"Calendar event {event_id} has a lesson")
    if not is_on_the_minute(event.starts_at):
        raise CalendarEventNotAssignableError(f"Calendar event {event_id} starts off the minute")
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
    # The assignment acknowledges the occurrence as it is - its reason included, which the rules give again at the
    # next import (``_check``); a title naming another student or subject is never acknowledged.
    event.title_name, event.changed, event.dismissed = None, False, True
    await session.flush()
    return lesson


async def dismiss_event(session: AsyncSession, event_id: uuid.UUID) -> None:
    """Take the occurrence off the list. Without a lesson it waits for nothing: the import proposes it no more. With
    one, the occurrence as last seen is acknowledged: a "no lesson" answer of the rules and a change already seen stay
    quiet until start, end, cancellation, subject, a new no-lesson reason or presence changes. A title naming another
    student or subject is never acknowledged: the next import flags it again. Idempotent."""
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
    """Store a returned occurrence as seen, then judge it: against its lesson, or - waiting - by the rules."""
    untouched = lesson is not None and _untouched(lesson, event)
    seen_before = _seen(event)
    title = parse_title(occurrence.title)
    event.starts_at, event.ends_at, event.cancelled = occurrence.starts_at, occurrence.ends_at, is_cancelled(occurrence)
    event.title_subject = title.subject if title is not None else None
    event.vanished, event.last_seen_at = False, now

    if lesson is None:
        if event.dismissed or event.changed:
            # Waits for the maintainer: dismissed, or flagged because it could not get its lesson - proposing it
            # again could add a second lesson once it moves away from the one that blocked it.
            event.title_name = None
        else:
            await _propose(session, event, occurrence, roster, counts)
        return

    # The rules' answer is part of what was seen: a "no lesson" answer by its reason, never by the name (PD13). A new
    # reason is a difference; an answer that turns into this lesson is not - an alias stored on assigning does that.
    reason_before = event.reason
    proposal = propose(occurrence, roster)
    event.reason = proposal.reason if isinstance(proposal, NoLesson) else None
    event.title_name = None
    differs = seen_before != _seen(event) or event.reason not in (None, reason_before)
    await _check(
        session,
        event,
        lesson,
        proposal,
        _named(proposal, occurrence, roster),
        counts,
        differs=differs,
        untouched=untouched,
    )


async def _check(
    session: AsyncSession,
    event: CalendarEvent,
    lesson: Lesson,
    proposal: Proposal,
    named: _Named | None,
    counts: ImportCounts,
    *,
    differs: bool,
    untouched: bool,
) -> None:
    """Judge an occurrence that has a lesson; the lesson itself changes only by following.

    - The title names another student or another subject (``named``) - by the rules' answer, or by its name where the
      rules stop before it: flagged at every import, whatever was acknowledged; once the lesson, the title or an alias
      settles it, a dismiss clears the flag.
    - The rules give no lesson, or the appointment changed and the lesson is touched: flagged, unless the maintainer
      acknowledged the occurrence as it is (``dismissed``: a dismiss, or the assignment - an assigned occurrence has
      no lesson by the rules). Any difference in start, end, cancellation, subject, the no-lesson reason or presence
      ends the acknowledgment.
    - Otherwise - this student, this subject, an untouched lesson - it follows a changed appointment.
    """
    if differs:
        event.dismissed = False
    if named is not None and (
        named.student_id != lesson.student_party_id or named.subject not in (None, lesson.subject)
    ):
        event.changed = True
        return
    match proposal:
        case NoLesson():
            if not event.dismissed:
                event.changed = True
            return
    if not differs:
        return
    if not untouched:
        event.changed = True
        return

    status = LessonStatus.CANCELLED if proposal.status is ProposedStatus.CANCELLED else None
    if (lesson.starts_at, lesson.minutes, status) == (event.starts_at, proposal.minutes, None):
        return
    try:
        await lessons.update_lesson(
            session, lesson.id, starts_at=event.starts_at, minutes=proposal.minutes, status=status
        )
    except LessonAlreadyExistsError:
        event.changed = True
        return
    counts.lessons_updated += 1


async def _propose(
    session: AsyncSession, event: CalendarEvent, occurrence: Occurrence, roster: Roster, counts: ImportCounts
) -> None:
    """Run the rules for an occurrence without a lesson; a lesson they propose is created through the lesson services.

    The name is kept only for the reasons that wait (``NoLesson.title``). Of the lesson services' errors two are the
    occurrence's, not the import's: a student who has a lesson at that start already - added by hand - gets no
    second one, and the occurrence is flagged ``changed`` until the maintainer assigns or dismisses it; a student who
    lost the role after the roster was read is ``unmatched``, as the rules answer from then on. Any other error is a
    bug and fails the whole import.
    """
    match propose(occurrence, roster):
        case NoLesson(reason=reason, title=title):
            event.reason = reason
            event.title_name = title.name if title is not None else None
        case LessonProposal() as proposal:
            event.reason, event.title_name = None, None
            try:
                lesson = await lessons.create_lesson(
                    session,
                    student_id=proposal.student_id,
                    subject=proposal.subject,
                    starts_at=occurrence.starts_at,
                    minutes=proposal.minutes,
                    calendar_event_id=event.id,
                )
            except LessonAlreadyExistsError:
                event.changed = True
                return
            except UnknownStudentError:
                title = parse_title(occurrence.title)
                event.reason = NoLessonReason.UNMATCHED
                event.title_name = title.name if title is not None else None
                return
            if proposal.status is ProposedStatus.CANCELLED:
                await lessons.update_lesson(session, lesson.id, status=LessonStatus.CANCELLED)
            counts.lessons_created += 1


async def _vanished(session: AsyncSession, event: CalendarEvent, lesson: Lesson | None, counts: ImportCounts) -> None:
    """The occurrence is gone from the range: so is an untouched lesson with it, and an occurrence without one. A
    touched lesson stays and its occurrence is flagged - once: an acknowledged vanishing is not flagged again."""
    if lesson is not None and not _untouched(lesson, event):
        if not event.vanished:
            event.vanished, event.dismissed, event.changed = True, False, True
        return

    if lesson is not None:
        await session.delete(lesson)
        await session.flush()
        counts.lessons_removed += 1
    await session.delete(event)
    counts.deleted += 1


def _untouched(lesson: Lesson, event: CalendarEvent) -> bool:
    """Whether the lesson is still as the import made it: ``planned``, without a note or an own rate, at the start and
    with the length of its occurrence as last seen. Any other lesson is touched: it never follows and never goes."""
    return (
        lesson.status is LessonStatus.PLANNED
        and lesson.note is None
        and lesson.rate_cents_per_hour is None
        and lesson.starts_at == event.starts_at
        and timedelta(minutes=lesson.minutes) == event.ends_at - event.starts_at
    )


@dataclass(frozen=True)
class _Named:
    """The student a title names, and its subject - ``None`` where the title holds none (a phone call, an all-day
    entry, a first meeting)."""

    student_id: uuid.UUID
    subject: str | None


def _named(proposal: Proposal, occurrence: Occurrence, roster: Roster) -> _Named | None:
    """Whom the title names: the rules' lesson, or - where they stop before the name (a duration or a time no lesson
    can have, a phone call, an all-day entry, a first meeting) - the one student its name matches. ``None`` for a name
    that matches nobody or several: PD13 keeps no name, so there is nothing to compare it with."""
    match proposal:
        case LessonProposal(student_id=student_id, subject=subject):
            return _Named(student_id, subject)
        case NoLesson(reason=reason):
            title = parse_title(occurrence.title)
            if title is None or not isinstance(student_id := match_name(title.name, roster), uuid.UUID):
                return None
            # Only a lesson title refused for its times (`duration`: an odd length or a time off the minute) has a
            # subject: before a phone call's, an all-day entry's or a first meeting's ` mit ` stands any word
            # ("Telefonat", "Ferien"; `Kennenlernen` is never read as a subject).
            return _Named(student_id, title.subject if reason is NoLessonReason.DURATION else None)


def _seen(event: CalendarEvent) -> tuple[object, ...]:
    """What a difference of an occurrence is measured on - never the name, which is not kept (PD13)."""
    return (event.starts_at, event.ends_at, event.cancelled, event.title_subject, event.vanished)


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
        # Postgres text holds no NUL character, and the CRM refuses one in its inputs: such a title is not read.
        title=event.subject if "\x00" not in event.subject else "",
        starts_at=event.start,
        ends_at=event.end,
        all_day=event.is_all_day,
        cancelled=event.is_cancelled,
        categories=event.categories,
        location=event.location,
        online_meeting=event.is_online_meeting,
    )


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
