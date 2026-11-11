"""The calendar import (billing spec, SF4-3), on Postgres, with Graph behind a mocked transport over G2 to G5.

The calendar proposes, a person confirms: an untouched `planned` lesson follows its appointment, a lesson that is not
`planned` never changes - its occurrence is flagged `changed` - and only an occurrence that starts inside the
imported range can vanish. Every name is synthetic.
"""

import datetime as dt
import uuid
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import CalendarEvent, Lesson, LessonStatus
from app.services.billing import calendar, calendar_aliases, lessons
from app.services.billing.calendar import ImportCounts
from app.services.billing.calendar_rules import NoLessonReason, Roster, StudentName
from app.services.billing.errors import CalendarUnavailableError
from app.services.crm import persons, roles
from tests.db.billing.calendar_support import (
    ERIKA,
    MAX,
    NOW,
    PAULA,
    answer_view,
    fixture_events,
    graph_event,
    graph_settings,
    moved,
    new_student,
    reader,
    snapshot,
)
from tests.integrations.graph.fake_graph import CALENDARS, VIEW_PATH, Credential, FakeGraph, answer, fixture

pytestmark = pytest.mark.db

BERLIN = ZoneInfo("Europe/Berlin")
G2_DAYS = (dt.date(2026, 11, 23), dt.date(2026, 12, 13))
ASSIGNED_START = dt.datetime(2026, 12, 2, 15, tzinfo=dt.UTC)
# An hour after G2's occurrence of 2026-12-01.
START_LATER = dt.datetime(2026, 12, 1, 16, tzinfo=dt.UTC)
G2_MOVED = next(
    event["id"] for event in fixture_events("g2-series-first-read") if "2026-12-01" in event["start"]["dateTime"]
)


@pytest.fixture(scope="module")
def credential() -> Credential:
    return Credential.generate()


@pytest.fixture
def fake() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
def run(session: AsyncSession, fake: FakeGraph, credential: Credential):
    """Import a range through the real client on the fake; the fake answers whatever the test set."""

    async def _run(first_day: dt.date, last_day: dt.date, *, now: dt.datetime = NOW) -> ImportCounts:
        return await calendar.import_calendar(
            session, reader(fake, graph_settings(credential)), first_day=first_day, last_day=last_day, now=now
        )

    return _run


async def _lessons(session: AsyncSession) -> list[Lesson]:
    return list(
        await session.scalars(select(Lesson).order_by(Lesson.starts_at).execution_options(populate_existing=True))
    )


async def _event(session: AsyncSession, external_id: str) -> CalendarEvent:
    return (
        await session.scalars(
            select(CalendarEvent)
            .where(CalendarEvent.external_id == external_id)
            .execution_options(populate_existing=True)
        )
    ).one()


async def _lesson_of(session: AsyncSession, external_id: str) -> Lesson:
    event = await _event(session, external_id)
    return (
        await session.scalars(
            select(Lesson).where(Lesson.calendar_event_id == event.id).execution_options(populate_existing=True)
        )
    ).one()


def _berlin(lesson: Lesson) -> dt.datetime:
    return lesson.starts_at.astimezone(BERLIN).replace(tzinfo=None)


async def _tick(session: AsyncSession, lesson: Lesson) -> None:
    await lessons.update_lesson(session, lesson.id, status=LessonStatus.HELD)


async def _row(session: AsyncSession, lesson_id: uuid.UUID) -> tuple[str, str, str]:
    """The lesson's row, byte for byte (``snapshot``)."""
    return next(row for row in await snapshot(session) if row[0] == "lesson" and f'"id": "{lesson_id}"' in row[2])


# --- idempotent ---


async def test_importing_a_range_twice_changes_no_row(session: AsyncSession, fake: FakeGraph, run):
    """G2's first read twice, at the same instant: not one row is written again - its ctid stays."""
    await new_student(session, MAX)
    answer_view(fake, fixture_events("g2-series-first-read"))

    first = await run(*G2_DAYS)
    before = await snapshot(session)
    second = await run(*G2_DAYS)

    assert first == ImportCounts(occurrences=3, lessons_created=3)
    assert second == ImportCounts(occurrences=3)
    assert await snapshot(session) == before
    assert len(before) == 6


async def test_a_later_import_of_the_same_range_only_marks_the_occurrences_seen(
    session: AsyncSession, fake: FakeGraph, run
):
    await new_student(session, MAX)
    answer_view(fake, fixture_events("g2-series-first-read"))
    await run(*G2_DAYS)
    lessons_before = [row for row in await snapshot(session) if row[0] == "lesson"]
    events_before = list(await session.execute(select(CalendarEvent.__table__).order_by(CalendarEvent.external_id)))

    later = NOW + dt.timedelta(days=1)
    await run(*G2_DAYS, now=later)

    assert [row for row in await snapshot(session) if row[0] == "lesson"] == lessons_before
    events_after = list(await session.execute(select(CalendarEvent.__table__).order_by(CalendarEvent.external_id)))
    unchanged = [column for column in CalendarEvent.__table__.columns.keys() if column != "last_seen_at"]
    assert [[row._mapping[column] for column in unchanged] for row in events_after] == [
        [row._mapping[column] for column in unchanged] for row in events_before
    ]
    assert {row.last_seen_at for row in events_after} == {later}


async def test_the_moved_occurrence_of_g2_is_the_same_occurrence_and_its_planned_lesson_follows(
    session: AsyncSession, fake: FakeGraph, run
):
    """Q16: the occurrence keeps its id when it moves and becomes an `exception` - a move, not a new event."""
    await new_student(session, MAX)
    answer_view(fake, fixture_events("g2-series-first-read"))
    await run(*G2_DAYS)
    event_before = await _event(session, G2_MOVED)
    lesson_before = await _lesson_of(session, G2_MOVED)
    event_id, lesson_id = event_before.id, lesson_before.id

    answer_view(fake, fixture_events("g2-series-second-read"))
    counts = await run(*G2_DAYS)

    assert counts == ImportCounts(occurrences=3, lessons_updated=1)
    assert await session.scalar(select(func.count()).select_from(CalendarEvent)) == 3
    lesson = await _lesson_of(session, G2_MOVED)
    assert (lesson.id, lesson.calendar_event_id) == (lesson_id, event_id)
    assert (_berlin(lesson), lesson.minutes, lesson.status) == (dt.datetime(2026, 12, 3, 15), 60, LessonStatus.PLANNED)
    event = await _event(session, G2_MOVED)
    assert (event.starts_at, event.changed) == (dt.datetime(2026, 12, 3, 14, tzinfo=dt.UTC), False)


# --- a touched lesson never changes ---


async def test_a_ticked_lesson_whose_appointment_moved_is_unchanged_and_its_occurrence_flagged(
    session: AsyncSession, fake: FakeGraph, run
):
    await new_student(session, MAX)
    answer_view(fake, fixture_events("g2-series-first-read"))
    await run(*G2_DAYS)
    ticked = await _lesson_of(session, G2_MOVED)
    await _tick(session, ticked)
    ticked_id = ticked.id
    row = await _row(session, ticked_id)

    answer_view(fake, fixture_events("g2-series-second-read"))
    counts = await run(*G2_DAYS)

    assert await _row(session, ticked_id) == row
    assert counts.lessons_updated == 0 and counts.changed == 1
    event = await _event(session, G2_MOVED)
    assert (event.changed, event.starts_at) == (True, dt.datetime(2026, 12, 3, 14, tzinfo=dt.UTC))


async def test_a_ticked_lesson_whose_appointment_shrank_is_unchanged_and_its_occurrence_flagged(
    session: AsyncSession, fake: FakeGraph, run
):
    await new_student(session, MAX)
    first_read = fixture_events("g2-series-first-read")
    answer_view(fake, first_read)
    await run(*G2_DAYS)
    ticked = await _lesson_of(session, G2_MOVED)
    await _tick(session, ticked)
    ticked_id = ticked.id
    row = await _row(session, ticked_id)

    answer_view(fake, [moved(event, minutes=45) if event["id"] == G2_MOVED else event for event in first_read])
    await run(*G2_DAYS)

    assert await _row(session, ticked_id) == row
    assert (await _event(session, G2_MOVED)).changed is True


async def test_a_ticked_lesson_whose_appointment_vanished_stays_and_its_occurrence_is_flagged(
    session: AsyncSession, fake: FakeGraph, run
):
    await new_student(session, MAX)
    first_read = fixture_events("g2-series-first-read")
    answer_view(fake, first_read)
    await run(*G2_DAYS)
    ticked = await _lesson_of(session, G2_MOVED)
    await _tick(session, ticked)
    ticked_id = ticked.id
    row = await _row(session, ticked_id)

    answer_view(fake, [event for event in first_read if event["id"] != G2_MOVED])
    counts = await run(*G2_DAYS)

    assert await _row(session, ticked_id) == row
    assert (counts.lessons_removed, counts.changed) == (0, 1)
    event = await _event(session, G2_MOVED)
    assert (event.changed, event.last_seen_at) == (True, NOW)


async def test_a_cancelled_lesson_the_import_created_does_not_follow_a_removed_category(
    session: AsyncSession, fake: FakeGraph, run
):
    """Fail safe: the category only presets the status; once the lesson is not `planned`, a person decides."""
    await new_student(session, MAX)
    start = dt.datetime(2026, 12, 2, 15, tzinfo=dt.UTC)
    answer_view(fake, [graph_event("late", "Mathe mit Max Mustermann", start, categories=["Abgesagt"])])
    await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))
    cancelled = await _lesson_of(session, "late")
    cancelled_id = cancelled.id
    row = await _row(session, cancelled_id)

    answer_view(fake, [graph_event("late", "Mathe mit Max Mustermann", start)])
    await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))

    assert await _row(session, cancelled_id) == row
    assert cancelled.status is LessonStatus.CANCELLED
    assert (await _event(session, "late")).changed is True


async def test_a_dismissed_difference_stays_dismissed_until_the_appointment_changes_again(
    session: AsyncSession, fake: FakeGraph, run
):
    await new_student(session, MAX)
    first_read = fixture_events("g2-series-first-read")
    answer_view(fake, first_read)
    await run(*G2_DAYS)
    await _tick(session, await _lesson_of(session, G2_MOVED))
    answer_view(fake, fixture_events("g2-series-second-read"))
    await run(*G2_DAYS)

    await calendar.dismiss_event(session, (await _event(session, G2_MOVED)).id)
    await run(*G2_DAYS)
    assert (await _event(session, G2_MOVED)).changed is False

    answer_view(fake, [event for event in first_read if event["id"] != G2_MOVED])
    await run(*G2_DAYS)
    assert (await _event(session, G2_MOVED)).changed is True, "a vanishing after a dismissed move is flagged"

    await calendar.dismiss_event(session, (await _event(session, G2_MOVED)).id)
    await run(*G2_DAYS)
    assert (await _event(session, G2_MOVED)).changed is False, "an acknowledged vanishing is not flagged again"


# --- an untouched lesson follows ---


async def test_a_planned_lesson_nobody_touched_goes_with_its_vanished_appointment(
    session: AsyncSession, fake: FakeGraph, run
):
    await new_student(session, MAX)
    first_read = fixture_events("g2-series-first-read")
    answer_view(fake, first_read)
    await run(*G2_DAYS)

    answer_view(fake, [event for event in first_read if event["id"] != G2_MOVED])
    counts = await run(*G2_DAYS)

    assert (counts.lessons_removed, counts.deleted) == (1, 1)
    assert [_berlin(lesson).date() for lesson in await _lessons(session)] == [
        dt.date(2026, 11, 24),
        dt.date(2026, 12, 8),
    ]
    assert G2_MOVED not in set(await session.scalars(select(CalendarEvent.external_id)))


async def test_a_planned_lesson_follows_a_late_cancellation_and_is_then_touched(
    session: AsyncSession, fake: FakeGraph, run
):
    await new_student(session, MAX)
    start = dt.datetime(2026, 12, 2, 15, tzinfo=dt.UTC)
    answer_view(fake, [graph_event("lesson", "Mathe mit Max Mustermann", start)])
    await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))

    answer_view(fake, [graph_event("lesson", "Mathe mit Max Mustermann", start, categories=["Abgesagt"])])
    counts = await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))

    lesson = await _lesson_of(session, "lesson")
    assert counts.lessons_updated == 1
    assert (lesson.status, lesson.billable, lesson.payer_party_id) == (LessonStatus.CANCELLED, False, None)


@pytest.mark.parametrize("minutes", [50, 0, 24 * 60 + 15], ids=["off-the-quarter-hour", "no-length", "over-a-day"])
async def test_a_planned_lesson_that_cannot_follow_is_unchanged_and_flagged(
    session: AsyncSession, fake: FakeGraph, run, minutes: int
):
    await new_student(session, MAX)
    first_read = fixture_events("g2-series-first-read")
    answer_view(fake, first_read)
    await run(*G2_DAYS)
    planned = await _lesson_of(session, G2_MOVED)
    planned_id = planned.id
    row = await _row(session, planned_id)

    answer_view(fake, [moved(event, minutes=minutes) if event["id"] == G2_MOVED else event for event in first_read])
    await run(*G2_DAYS)

    assert await _row(session, planned_id) == row
    assert (await _event(session, G2_MOVED)).changed is True


async def test_a_planned_lesson_that_would_move_onto_another_lesson_is_unchanged_and_flagged(
    session: AsyncSession, fake: FakeGraph, run
):
    student = await new_student(session, MAX)
    answer_view(fake, fixture_events("g2-series-first-read"))
    await run(*G2_DAYS)
    planned = await _lesson_of(session, G2_MOVED)
    planned_id = planned.id
    row = await _row(session, planned_id)
    await lessons.create_lesson(
        session, student_id=student, subject="Mathe", starts_at=dt.datetime(2026, 12, 3, 14, tzinfo=dt.UTC), minutes=60
    )

    answer_view(fake, fixture_events("g2-series-second-read"))
    await run(*G2_DAYS)

    assert await _row(session, planned_id) == row
    assert (await _event(session, G2_MOVED)).changed is True


# --- the range ---


async def test_an_occurrence_outside_the_imported_range_never_vanishes(session: AsyncSession, fake: FakeGraph, run):
    await new_student(session, MAX)
    first_read = fixture_events("g2-series-first-read")
    answer_view(fake, first_read)
    await run(*G2_DAYS)
    before = await snapshot(session)

    # A later week the calendar has nothing in, and the first week alone, whose read lacks the later occurrences.
    answer_view(fake, [])
    assert await run(dt.date(2026, 12, 14), dt.date(2026, 12, 20)) == ImportCounts()
    answer_view(fake, first_read[:1])
    counts = await run(dt.date(2026, 11, 23), dt.date(2026, 11, 29))

    assert counts == ImportCounts(occurrences=1)
    assert await snapshot(session) == before


async def test_an_occurrence_that_starts_before_the_range_is_left_to_its_own_range(
    session: AsyncSession, fake: FakeGraph, run
):
    """`calendarView` also returns what only overlaps the range; the import takes what starts inside it."""
    await new_student(session, MAX)
    evening = dt.datetime(2026, 12, 1, 22, 30, tzinfo=dt.UTC)  # 23:30 to 00:30 in Berlin
    answer_view(fake, [graph_event("overlap", "Mathe mit Max Mustermann", evening)])

    assert await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2)) == ImportCounts()
    assert await snapshot(session) == []


# --- late cancellation (G3) and clock changes (G5) ---


async def test_a_late_cancellation_becomes_a_cancelled_lesson_that_is_not_billable(
    session: AsyncSession, fake: FakeGraph, run
):
    await new_student(session, PAULA)
    await new_student(session, ERIKA)
    answer_view(fake, fixture_events("g3-late-cancellation"))

    counts = await run(dt.date(2026, 11, 30), dt.date(2026, 12, 6))

    assert counts == ImportCounts(occurrences=2, lessons_created=2)
    assert [(lesson.status, lesson.billable, lesson.payer_party_id) for lesson in await _lessons(session)] == [
        (LessonStatus.CANCELLED, False, None),
        (LessonStatus.CANCELLED, False, None),
    ]
    assert {event.cancelled for event in await session.scalars(select(CalendarEvent))} == {True}


@pytest.mark.parametrize(
    ("name", "days", "expected"),
    [
        (
            "g5-spring-clock-change",
            (dt.date(2026, 3, 23), dt.date(2026, 4, 5)),
            [
                dt.datetime(2026, 3, 26, 17),
                dt.datetime(2026, 3, 29, 0, 30),
                dt.datetime(2026, 3, 29, 23, 30),
                dt.datetime(2026, 4, 2, 17),
            ],
        ),
        (
            "g5-autumn-clock-change",
            (dt.date(2026, 10, 19), dt.date(2026, 11, 1)),
            [
                dt.datetime(2026, 10, 22, 17),
                dt.datetime(2026, 10, 25, 0, 30),
                dt.datetime(2026, 10, 25, 23, 30),
                dt.datetime(2026, 10, 29, 17),
            ],
        ),
    ],
    ids=["spring", "autumn"],
)
async def test_lessons_in_the_weeks_of_the_clock_changes_land_on_their_berlin_day_and_time(
    session: AsyncSession, fake: FakeGraph, run, name: str, days: tuple[dt.date, dt.date], expected: list[dt.datetime]
):
    for student in (MAX, ERIKA, PAULA):
        await new_student(session, student)
    answer_view(fake, fixture_events(name))

    await run(*days)

    assert [_berlin(lesson) for lesson in await _lessons(session)] == expected
    # The range asked for is the fixture's: from Berlin midnight to Berlin midnight, across the change.
    view = next(request for request in fake.requests if request.url.path == VIEW_PATH)
    params = fixture(name)["request"]["params"]
    assert (view.url.params["startDateTime"], view.url.params["endDateTime"]) == (
        params["startDateTime"],
        params["endDateTime"],
    )


@pytest.mark.parametrize(
    ("name", "day"),
    [("g5-spring-clock-change", dt.date(2026, 3, 29)), ("g5-autumn-clock-change", dt.date(2026, 10, 25))],
    ids=["spring", "autumn"],
)
async def test_the_day_of_the_clock_change_holds_its_0030_and_its_2330_lesson(
    session: AsyncSession, fake: FakeGraph, run, name: str, day: dt.date
):
    for student in (MAX, ERIKA, PAULA):
        await new_student(session, student)
    answer_view(fake, fixture_events(name))

    await run(day, day)

    assert [_berlin(lesson) for lesson in await _lessons(session)] == [
        dt.datetime.combine(day, dt.time(0, 30)),
        dt.datetime.combine(day, dt.time(23, 30)),
    ]
    assert await lessons.list_lessons(session, limit=10, offset=0, from_day=day, to_day=day) == (
        await _lessons(session),
        2,
    )


# --- matching and collisions ---


async def test_an_occurrence_whose_student_has_a_lesson_at_that_start_gets_none_and_is_flagged(
    session: AsyncSession, fake: FakeGraph, run
):
    student = await new_student(session, MAX)
    by_hand = await lessons.create_lesson(
        session, student_id=student, subject="Physik", starts_at=dt.datetime(2026, 12, 1, 15, tzinfo=dt.UTC), minutes=45
    )
    by_hand_id = by_hand.id
    row = await _row(session, by_hand_id)
    answer_view(fake, fixture_events("g2-series-first-read"))

    counts = await run(*G2_DAYS)

    assert (counts.lessons_created, counts.changed) == (2, 1)
    assert await _row(session, by_hand_id) == row
    event = await _event(session, G2_MOVED)
    assert (event.changed, event.reason, event.title_name) == (True, None, None)
    assert await session.scalar(select(func.count()).where(Lesson.calendar_event_id == event.id)) == 0


async def test_an_occurrence_with_a_lesson_keeps_its_student_and_is_flagged_when_the_match_changes(
    session: AsyncSession, fake: FakeGraph, run
):
    await new_student(session, MAX)
    other = await new_student(session, ("Moritz", "Beispiel"))
    answer_view(fake, fixture_events("g2-series-first-read"))
    await run(*G2_DAYS)
    rows = [row for row in await snapshot(session) if row[0] == "lesson"]

    await calendar_aliases.create_alias(session, name="Max Mustermann", student_id=other)
    counts = await run(*G2_DAYS)

    assert [row for row in await snapshot(session) if row[0] == "lesson"] == rows
    assert (counts.lessons_updated, counts.changed) == (0, 3)


async def test_the_roster_holds_every_student_beyond_the_first_page(
    session: AsyncSession, fake: FakeGraph, run, monkeypatch: pytest.MonkeyPatch
):
    """With pages of one student the roster is read in three pages; Max matches wherever he lands."""
    monkeypatch.setattr(calendar, "_ROSTER_PAGE", 1)
    await new_student(session, ("Moritz", "Beispiel"))
    await new_student(session, PAULA)
    await new_student(session, MAX)
    answer_view(fake, fixture_events("g2-series-first-read"))

    counts = await run(*G2_DAYS)

    assert (counts.lessons_created, counts.unassigned) == (3, 0)


# --- PD13: what is kept of a name ---


async def test_the_name_is_stored_while_an_occurrence_waits_for_an_assignment(
    session: AsyncSession, fake: FakeGraph, run
):
    start = dt.datetime(2026, 12, 2, 15, tzinfo=dt.UTC)
    answer_view(
        fake,
        [
            graph_event("unmatched", "Mathe mit Student Z", start),
            graph_event("duration", "Mathe mit Student Y", start, minutes=50),
            graph_event("no-pattern", "Vorbereitung", start),
        ],
    )

    counts = await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))

    assert counts == ImportCounts(occurrences=3, unassigned=3)
    stored = {
        event.external_id: (event.reason, event.title_name) for event in await session.scalars(select(CalendarEvent))
    }
    assert stored == {
        "unmatched": (NoLessonReason.UNMATCHED, "Student Z"),
        "duration": (NoLessonReason.DURATION, "Student Y"),
        "no-pattern": (NoLessonReason.NO_TITLE_PATTERN, None),
    }


async def test_the_name_is_never_stored_for_all_day_phone_and_first_meeting_occurrences(
    session: AsyncSession, fake: FakeGraph, run
):
    answer_view(fake, fixture_events("g4-special-entries"))

    await run(dt.date(2026, 12, 7), dt.date(2026, 12, 13))

    stored = {event.reason: event.title_name for event in await session.scalars(select(CalendarEvent))}
    assert stored == {
        NoLessonReason.UNMATCHED: "Max Mustermann",  # the Teams lesson: no student holds the name here
        NoLessonReason.PHONE: None,
        NoLessonReason.ALL_DAY: None,
        NoLessonReason.FIRST_MEETING: None,
        NoLessonReason.NO_TITLE_PATTERN: None,
    }


async def test_a_first_meeting_beyond_the_exact_subject_keeps_no_name_and_gets_no_lesson(
    session: AsyncSession, fake: FakeGraph, run
):
    """`Erstes Kennenlernen mit <Name>` with a name no student holds: no lesson, and the name is not kept (PD13)."""
    await new_student(session, MAX)
    start = dt.datetime(2026, 12, 2, 15, tzinfo=dt.UTC)
    answer_view(fake, [graph_event("first-meeting", "Erstes Kennenlernen mit Student Z", start)])

    counts = await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))

    event = await _event(session, "first-meeting")
    assert (event.reason, event.title_name) == (NoLessonReason.FIRST_MEETING, None)
    assert counts == ImportCounts(occurrences=1)
    assert await session.scalar(select(func.count()).select_from(Lesson)) == 0


async def test_the_name_is_cleared_when_the_import_matches_the_occurrence(session: AsyncSession, fake: FakeGraph, run):
    student = await new_student(session, ("Moritz", "Beispiel"))
    answer_view(fake, [graph_event("nickname", "Mathe mit Mo", dt.datetime(2026, 12, 2, 15, tzinfo=dt.UTC))])
    await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))
    assert (await _event(session, "nickname")).title_name == "Mo"

    await calendar_aliases.create_alias(session, name="Mo", student_id=student)
    counts = await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))

    event = await _event(session, "nickname")
    assert counts.lessons_created == 1
    assert (event.title_name, event.reason) == (None, None)
    assert (await _lesson_of(session, "nickname")).student_party_id == student


async def test_a_matched_occurrence_never_stores_the_name(session: AsyncSession, fake: FakeGraph, run):
    await new_student(session, MAX)
    answer_view(fake, fixture_events("g2-series-first-read"))

    await run(*G2_DAYS)

    assert set(await session.scalars(select(CalendarEvent.title_name))) == {None}


async def test_the_name_is_cleared_when_the_occurrence_is_assigned_or_dismissed(
    session: AsyncSession, fake: FakeGraph, run
):
    student = await new_student(session, ("Moritz", "Beispiel"))
    start = dt.datetime(2026, 12, 2, 15, tzinfo=dt.UTC)
    answer_view(fake, [graph_event("assigned", "Mathe mit Mo", start), graph_event("dismissed", "Mathe mit Jo", start)])
    await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))

    await calendar.assign_event(
        session, (await _event(session, "assigned")).id, student_id=student, subject="Mathe", minutes=60
    )
    await calendar.dismiss_event(session, (await _event(session, "dismissed")).id)
    await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))

    assert set(await session.scalars(select(CalendarEvent.title_name))) == {None}
    assert (await _event(session, "dismissed")).dismissed is True
    assert await session.scalar(select(func.count()).select_from(Lesson)) == 1


async def test_a_dismissed_occurrence_is_not_proposed_again(session: AsyncSession, fake: FakeGraph, run):
    student = await new_student(session, ("Moritz", "Beispiel"))
    answer_view(fake, [graph_event("nickname", "Mathe mit Mo", dt.datetime(2026, 12, 2, 15, tzinfo=dt.UTC))])
    await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))
    await calendar.dismiss_event(session, (await _event(session, "nickname")).id)

    await calendar_aliases.create_alias(session, name="Mo", student_id=student)
    counts = await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))

    assert (counts.lessons_created, counts.unassigned) == (0, 0)
    assert await session.scalar(select(func.count()).select_from(Lesson)) == 0


OLD_DAY = dt.date(2026, 8, 1)


async def _old_occurrences(session: AsyncSession, fake: FakeGraph, run) -> None:
    """On `OLD_DAY`: one waiting, one dismissed, one with a lesson."""
    await new_student(session, MAX)
    start = dt.datetime.combine(OLD_DAY, dt.time(10), tzinfo=BERLIN)
    answer_view(
        fake,
        [
            graph_event("waiting", "Mathe mit Student Z", start),
            graph_event("dismissed", "Mathe mit Student Y", start),
            graph_event("lesson", "Mathe mit Max Mustermann", start),
        ],
    )
    await run(OLD_DAY, OLD_DAY, now=start)
    await calendar.dismiss_event(session, (await _event(session, "dismissed")).id)
    answer_view(fake, [])


async def test_an_occurrence_without_a_lesson_is_kept_until_90_days_after_its_start(
    session: AsyncSession, fake: FakeGraph, run
):
    await _old_occurrences(session, fake, run)
    last_moment = dt.datetime.combine(OLD_DAY + dt.timedelta(days=89), dt.time(23, 59), tzinfo=BERLIN)

    counts = await run(dt.date(2026, 12, 1), dt.date(2026, 12, 1), now=last_moment)

    assert counts.deleted == 0
    assert set(await session.scalars(select(CalendarEvent.external_id))) == {"waiting", "dismissed", "lesson"}


async def test_unassigned_and_dismissed_occurrences_are_deleted_90_days_after_their_start_with_the_next_import(
    session: AsyncSession, fake: FakeGraph, run
):
    await _old_occurrences(session, fake, run)
    first_moment = dt.datetime.combine(OLD_DAY + dt.timedelta(days=90), dt.time(0), tzinfo=BERLIN)

    counts = await run(dt.date(2026, 12, 1), dt.date(2026, 12, 1), now=first_moment)

    assert counts.deleted == 2
    assert set(await session.scalars(select(CalendarEvent.external_id))) == {"lesson"}


async def test_an_occurrence_with_a_lesson_is_never_deleted_by_the_retention_rule(
    session: AsyncSession, fake: FakeGraph, run
):
    await _old_occurrences(session, fake, run)
    lesson = await _lesson_of(session, "lesson")

    await run(dt.date(2026, 12, 1), dt.date(2026, 12, 1), now=dt.datetime(2030, 1, 1, tzinfo=dt.UTC))

    assert await _lesson_of(session, "lesson") == lesson


# --- a failed read ---


@pytest.mark.parametrize(
    "failure",
    ["refused", "identity", "unavailable", "unexpected", "throttled"],
)
async def test_a_failed_read_is_calendar_unavailable_and_changes_no_row(
    session: AsyncSession, fake: FakeGraph, run, failure: str
):
    """The read comes before any write: a waiting occurrence that the failed import's ``now`` makes 90 days old
    would be deleted by a retention run before the read."""
    await new_student(session, MAX)
    waiting = graph_event("waiting", "Mathe mit Student Z", dt.datetime(2026, 12, 2, 15, tzinfo=dt.UTC))
    answer_view(fake, [*fixture_events("g2-series-first-read"), waiting])
    await run(*G2_DAYS)
    before = await snapshot(session)
    match failure:
        case "refused":
            fake.on("GET", VIEW_PATH, answer("g6-mailbox-outside-scope"))
        case "identity":
            fake.on("GET", CALENDARS["request"]["path"], httpx.Response(200, json={"value": []}))
        case "unavailable":
            fake.on("GET", VIEW_PATH, httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}}))
        case "unexpected":
            fake.on("GET", VIEW_PATH, httpx.Response(200, json={"value": [{"subject": "Mathe mit Max Mustermann"}]}))
        case "throttled":
            fake.on("GET", VIEW_PATH, httpx.Response(429, json={"error": {"code": "TooManyRequests"}}))

    with pytest.raises(CalendarUnavailableError) as raised:
        await run(*G2_DAYS, now=NOW + dt.timedelta(days=200))

    assert await snapshot(session) == before
    assert "Mustermann" not in str(raised.value) and raised.value.__cause__ is None


# --- an occurrence with a lesson is judged by the rules again ---


def _berlin_midnight_utc(day: dt.date) -> str:
    return dt.datetime.combine(day, dt.time(), tzinfo=BERLIN).astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000")


def _turned(event: dict, variant: str) -> dict:
    """The appointment of ``event`` as it might be edited: no longer this student's lesson of this subject."""
    match variant:
        case "all-day":
            start = {"dateTime": _berlin_midnight_utc(dt.date(2026, 12, 1)), "timeZone": "UTC"}
            end = {"dateTime": _berlin_midnight_utc(dt.date(2026, 12, 2)), "timeZone": "UTC"}
            return {**event, "isAllDay": True, "start": start, "end": end}
        case "phone-category":
            return {**event, "categories": ["Telefon"]}
        case "phone-location":
            return {**event, "location": {"displayName": "+49 30 1234567"}}
        case "first-meeting":
            return {**event, "subject": "Kennenlernen mit Max Mustermann"}
        case "no-title-pattern":
            return {**event, "subject": "Max Mustermann"}
        case "other-student":
            return {**event, "subject": "Mathematik mit Paula Beispiel"}
        case "other-subject":
            return {**event, "subject": "Physik mit Max Mustermann"}
    raise AssertionError(variant)


# Each turn with the reason the rules now give; `None` where they give a lesson - of another student or subject.
TURNS = {
    "all-day": NoLessonReason.ALL_DAY,
    "phone-category": NoLessonReason.PHONE,
    "phone-location": NoLessonReason.PHONE,
    "first-meeting": NoLessonReason.FIRST_MEETING,
    "no-title-pattern": NoLessonReason.NO_TITLE_PATTERN,
    "other-student": None,
    "other-subject": None,
}


@pytest.mark.parametrize("variant", list(TURNS))
async def test_a_planned_lesson_whose_appointment_is_no_longer_its_lesson_stays_and_is_flagged(
    session: AsyncSession, fake: FakeGraph, run, variant: str
):
    """The rules still decide: an appointment they no longer give this student and subject is never followed - not
    to an all-day entry, not to another student - and the lesson stays exactly as it is, for the maintainer."""
    await new_student(session, MAX)
    await new_student(session, PAULA)
    first_read = fixture_events("g2-series-first-read")
    answer_view(fake, first_read)
    await run(*G2_DAYS)
    planned_id = (await _lesson_of(session, G2_MOVED)).id
    row = await _row(session, planned_id)

    answer_view(fake, [_turned(event, variant) if event["id"] == G2_MOVED else event for event in first_read])
    counts = await run(*G2_DAYS)

    assert await _row(session, planned_id) == row
    assert (counts.lessons_created, counts.lessons_updated, counts.lessons_removed, counts.changed) == (0, 0, 0, 1)
    event = await _event(session, G2_MOVED)
    assert (event.changed, event.title_name, event.reason) == (True, None, TURNS[variant])

    before = await snapshot(session)
    await run(*G2_DAYS)
    assert await snapshot(session) == before, "the flag is set once; an unchanged calendar writes nothing"


async def test_another_subject_is_flagged_again_after_a_dismiss_until_the_lesson_settles_it(
    session: AsyncSession, fake: FakeGraph, run
):
    await new_student(session, MAX)
    first_read = fixture_events("g2-series-first-read")
    answer_view(fake, first_read)
    await run(*G2_DAYS)
    planned_id = (await _lesson_of(session, G2_MOVED)).id
    answer_view(fake, [_turned(event, "other-subject") if event["id"] == G2_MOVED else event for event in first_read])
    await run(*G2_DAYS)

    await calendar.dismiss_event(session, (await _event(session, G2_MOVED)).id)
    await run(*G2_DAYS)
    assert (await _event(session, G2_MOVED)).changed is True, "another subject is never acknowledged"

    await lessons.update_lesson(session, planned_id, subject="Physik")
    await calendar.dismiss_event(session, (await _event(session, G2_MOVED)).id)
    await run(*G2_DAYS)
    assert (await _event(session, G2_MOVED)).changed is False, "settled: the rules give this lesson again"


async def _assigned_to(session: AsyncSession, run, fake: FakeGraph, title: str, student: uuid.UUID) -> uuid.UUID:
    """An occurrence of 2026-12-02 named ``title`` that waits, assigned to ``student``; returns the lesson's id."""
    answer_view(fake, [graph_event("assigned", title, ASSIGNED_START)])
    await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))
    event_id = (await _event(session, "assigned")).id
    lesson = await calendar.assign_event(session, event_id, student_id=student, subject="Mathe", minutes=60)
    return lesson.id


async def test_an_assigned_occurrence_whose_title_names_another_student_is_flagged(
    session: AsyncSession, fake: FakeGraph, run
):
    """P1: same time and subject, only the name changes - to a student of the roster. The acknowledgment of the
    assignment covered "nobody matches", never another student."""
    max_id = await new_student(session, MAX)
    await new_student(session, PAULA)
    lesson_id = await _assigned_to(session, run, fake, "Mathe mit Student Z", max_id)
    assert (await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))).changed == 0
    row = await _row(session, lesson_id)

    answer_view(fake, [graph_event("assigned", "Mathe mit Paula Beispiel", ASSIGNED_START)])
    counts = await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))

    assert (counts.changed, counts.lessons_created) == (1, 0)
    assert await _row(session, lesson_id) == row
    before = await snapshot(session)
    await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))
    assert await snapshot(session) == before, "flagged once; the repeat import writes nothing"


async def test_an_ambiguous_name_assigned_to_one_namesake_is_flagged_when_it_names_the_other_alone(
    session: AsyncSession, fake: FakeGraph, run
):
    """P1b: the namesake the lesson went to loses the student role; the title now means the other one alone."""
    first = await new_student(session, MAX)
    await new_student(session, MAX)
    lesson_id = await _assigned_to(session, run, fake, "Mathe mit Max Mustermann", first)
    assert (await _event(session, "assigned")).reason == NoLessonReason.AMBIGUOUS
    assert (await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))).changed == 0
    row = await _row(session, lesson_id)

    await roles.remove_student_role(session, first)
    counts = await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))

    assert counts.changed == 1
    assert await _row(session, lesson_id) == row


async def test_a_dismissed_other_student_is_flagged_again_when_the_title_names_a_third(
    session: AsyncSession, fake: FakeGraph, run
):
    """P2: a dismiss never covers another student - not the one it was given for, not a third one."""
    await new_student(session, MAX)
    await new_student(session, PAULA)
    await new_student(session, ERIKA)
    first_read = fixture_events("g2-series-first-read")
    answer_view(fake, first_read)
    await run(*G2_DAYS)
    planned_id = (await _lesson_of(session, G2_MOVED)).id
    row = await _row(session, planned_id)
    answer_view(fake, [_turned(event, "other-student") if event["id"] == G2_MOVED else event for event in first_read])
    await run(*G2_DAYS)
    await calendar.dismiss_event(session, (await _event(session, G2_MOVED)).id)

    third = {**next(e for e in first_read if e["id"] == G2_MOVED), "subject": "Mathematik mit Erika Mustermann"}
    answer_view(fake, [third if event["id"] == G2_MOVED else event for event in first_read])
    counts = await run(*G2_DAYS)

    assert (counts.changed, counts.lessons_updated) == (1, 0)
    assert await _row(session, planned_id) == row


async def test_an_assigned_occurrence_of_an_odd_length_whose_title_names_another_student_is_flagged(
    session: AsyncSession, fake: FakeGraph, run
):
    """Q2: the rules stop at the duration before they read the name; the name is matched for the lesson anyway."""
    max_id = await new_student(session, MAX)
    await new_student(session, PAULA)
    answer_view(fake, [graph_event("odd", "Mathe mit Max Mustermann", ASSIGNED_START, minutes=50)])
    await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))
    event_id = (await _event(session, "odd")).id
    lesson = await calendar.assign_event(session, event_id, student_id=max_id, subject="Mathe", minutes=45)
    lesson_id = lesson.id
    assert (await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))).changed == 0
    row = await _row(session, lesson_id)

    answer_view(fake, [graph_event("odd", "Mathe mit Paula Beispiel", ASSIGNED_START, minutes=50)])
    counts = await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))

    assert counts.changed == 1
    assert await _row(session, lesson_id) == row
    before = await snapshot(session)
    await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))
    assert await snapshot(session) == before


@pytest.mark.parametrize(
    ("variant", "other"),
    [
        ("phone-category", {"subject": "Mathematik mit Paula Beispiel"}),
        ("all-day", {"subject": "Mathematik mit Paula Beispiel"}),
        ("first-meeting", {"subject": "Kennenlernen mit Paula Beispiel"}),
    ],
    ids=["phone", "all-day", "first-meeting"],
)
async def test_a_dismissed_no_lesson_flag_is_flagged_again_when_the_title_names_another_student(
    session: AsyncSession, fake: FakeGraph, run, variant: str, other: dict
):
    """Q3, Q3b: a dismissed phone, all-day or first-meeting flag stays quiet while the title names the lesson's student;
    the rules stop before the name, the name is matched anyway - another student is never acknowledged."""
    await new_student(session, MAX)
    await new_student(session, PAULA)
    first_read = fixture_events("g2-series-first-read")
    answer_view(fake, first_read)
    await run(*G2_DAYS)
    planned_id = (await _lesson_of(session, G2_MOVED)).id
    row = await _row(session, planned_id)
    answer_view(fake, [_turned(event, variant) if event["id"] == G2_MOVED else event for event in first_read])
    await run(*G2_DAYS)
    await calendar.dismiss_event(session, (await _event(session, G2_MOVED)).id)
    assert (await run(*G2_DAYS)).changed == 0, "the lesson's own student: the acknowledgment holds"

    answer_view(
        fake, [{**_turned(event, variant), **other} if event["id"] == G2_MOVED else event for event in first_read]
    )
    counts = await run(*G2_DAYS)

    assert counts.changed == 1
    assert await _row(session, planned_id) == row


@pytest.mark.parametrize(
    ("variant", "subject"),
    [("phone-category", "Telefonat mit Max Mustermann"), ("all-day", "Ferien mit Max Mustermann")],
    ids=["phone", "all-day"],
)
async def test_a_phone_call_or_all_day_entry_of_the_lessons_own_student_can_be_acknowledged(
    session: AsyncSession, fake: FakeGraph, run, variant: str, subject: str
):
    """The word before ` mit ` of a phone call or an all-day entry is no subject: only the student is compared, so a
    dismiss holds, and a repeat import writes nothing."""
    await new_student(session, MAX)
    first_read = fixture_events("g2-series-first-read")
    answer_view(fake, first_read)
    await run(*G2_DAYS)
    planned_id = (await _lesson_of(session, G2_MOVED)).id
    row = await _row(session, planned_id)
    turned = [
        {**_turned(event, variant), "subject": subject} if event["id"] == G2_MOVED else event for event in first_read
    ]
    answer_view(fake, turned)
    assert (await run(*G2_DAYS)).changed == 1

    await calendar.dismiss_event(session, (await _event(session, G2_MOVED)).id)
    assert (await run(*G2_DAYS)).changed == 0
    before = await snapshot(session)
    await run(*G2_DAYS)

    assert await snapshot(session) == before
    assert await _row(session, planned_id) == row


async def test_another_subject_behind_an_odd_length_is_flagged_again_after_a_dismiss(
    session: AsyncSession, fake: FakeGraph, run
):
    """A lesson title of an odd length is still a lesson title: its subject counts, and is never acknowledged."""
    max_id = await new_student(session, MAX)
    answer_view(fake, [graph_event("odd", "Mathe mit Max Mustermann", ASSIGNED_START, minutes=50)])
    await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))
    lesson = await calendar.assign_event(
        session, (await _event(session, "odd")).id, student_id=max_id, subject="Mathe", minutes=45
    )
    lesson_id = lesson.id
    row = await _row(session, lesson_id)
    answer_view(fake, [graph_event("odd", "Physik mit Max Mustermann", ASSIGNED_START, minutes=50)])
    await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))

    await calendar.dismiss_event(session, (await _event(session, "odd")).id)
    counts = await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))

    assert counts.changed == 1
    assert await _row(session, lesson_id) == row


async def test_a_new_no_lesson_reason_after_an_assignment_is_flagged(session: AsyncSession, fake: FakeGraph, run):
    """N19: an assigned occurrence whose appointment becomes a phone call - start, length and subject unchanged."""
    student = await new_student(session, MAX)
    lesson_id = await _assigned_to(session, run, fake, "Mathe mit Student Z", student)
    row = await _row(session, lesson_id)

    answer_view(fake, [graph_event("assigned", "Mathe mit Student Z", ASSIGNED_START, categories=["Telefon"])])
    counts = await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))

    assert counts.changed == 1
    assert (await _event(session, "assigned")).reason == NoLessonReason.PHONE
    assert await _row(session, lesson_id) == row


async def test_a_subject_change_after_an_acknowledgment_is_flagged(session: AsyncSession, fake: FakeGraph, run):
    """The subject is part of what was seen: an assigned occurrence whose subject changes is the maintainer's again,
    although the rules still match nobody."""
    student = await new_student(session, MAX)
    lesson_id = await _assigned_to(session, run, fake, "Mathe mit Student Z", student)
    row = await _row(session, lesson_id)

    answer_view(fake, [graph_event("assigned", "Physik mit Student Z", ASSIGNED_START)])
    counts = await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))

    assert counts.changed == 1
    assert await _row(session, lesson_id) == row


async def test_an_unchanged_touched_lesson_is_not_flagged(session: AsyncSession, fake: FakeGraph, run):
    """A ticked lesson whose appointment did not change is no difference: the list does not fill with every tick."""
    await new_student(session, MAX)
    answer_view(fake, fixture_events("g2-series-first-read"))
    await run(*G2_DAYS)
    await _tick(session, await _lesson_of(session, G2_MOVED))
    before = await snapshot(session)

    counts = await run(*G2_DAYS)

    assert counts.changed == 0
    assert await snapshot(session) == before


async def test_an_assigned_lesson_is_not_flagged_by_the_rules_until_its_appointment_changes(
    session: AsyncSession, fake: FakeGraph, run
):
    """The assignment acknowledges what the rules cannot see; a later change is the maintainer's again."""
    student = await new_student(session, MAX)
    start = dt.datetime(2026, 12, 2, 15, tzinfo=dt.UTC)
    waiting = graph_event("waiting", "Mathe mit Student Z", start)
    answer_view(fake, [waiting])
    await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))
    lesson = await calendar.assign_event(
        session, (await _event(session, "waiting")).id, student_id=student, subject="Mathe", minutes=60
    )
    lesson_id = lesson.id
    row = await _row(session, lesson_id)

    counts = await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))
    assert (counts.changed, (await _event(session, "waiting")).changed) == (0, False)

    answer_view(fake, [moved(waiting, start + dt.timedelta(hours=1))])
    counts = await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))
    assert (counts.lessons_updated, (await _event(session, "waiting")).changed) == (0, True)
    assert await _row(session, lesson_id) == row


# --- an occurrence that could not get its lesson waits for the maintainer ---


async def test_a_flagged_collision_is_not_proposed_again_when_its_appointment_moves(
    session: AsyncSession, fake: FakeGraph, run
):
    student = await new_student(session, MAX)
    await lessons.create_lesson(
        session, student_id=student, subject="Physik", starts_at=dt.datetime(2026, 12, 1, 15, tzinfo=dt.UTC), minutes=45
    )
    first_read = fixture_events("g2-series-first-read")
    answer_view(fake, first_read)
    await run(*G2_DAYS)

    answer_view(fake, [moved(event, START_LATER) if event["id"] == G2_MOVED else event for event in first_read])
    counts = await run(*G2_DAYS)

    assert (counts.lessons_created, counts.changed) == (0, 1)
    event = await _event(session, G2_MOVED)
    assert (event.changed, event.starts_at) == (True, START_LATER)
    assert await session.scalar(select(func.count()).where(Lesson.calendar_event_id == event.id)) == 0
    on_that_day = [lesson for lesson in await _lessons(session) if _berlin(lesson).date() == dt.date(2026, 12, 1)]
    assert [(lesson.subject, lesson.calendar_event_id) for lesson in on_that_day] == [("Physik", None)]


# --- a former student ---


async def test_an_alias_of_a_former_student_leaves_its_occurrence_unmatched_and_the_import_goes_on(
    session: AsyncSession, fake: FakeGraph, run
):
    await new_student(session, MAX)
    former = await new_student(session, ("Moritz", "Beispiel"))
    await calendar_aliases.create_alias(session, name="Mo", student_id=former)
    await roles.remove_student_role(session, former)
    start = dt.datetime(2026, 12, 2, 15, tzinfo=dt.UTC)
    answer_view(fake, [*fixture_events("g2-series-first-read"), graph_event("mo", "Mathe mit Mo", start)])

    counts = await run(*G2_DAYS)

    assert (counts.lessons_created, counts.unassigned) == (3, 1)
    event = await _event(session, "mo")
    assert (event.reason, event.title_name) == (NoLessonReason.UNMATCHED, "Mo")


async def test_a_student_who_lost_the_role_after_the_roster_was_read_is_unmatched(
    session: AsyncSession, fake: FakeGraph, run, monkeypatch: pytest.MonkeyPatch
):
    """The lesson services refuse a party that is no student (``unknown_student``): the occurrence waits as
    ``unmatched``, as the rules answer from then on - the import does not fail on it."""
    no_student = await persons.create_person(session, firstname="Max", lastname="Mustermann")

    async def stale_roster(_session: AsyncSession) -> Roster:
        return Roster(students=(StudentName(no_student.id, "Max", "Mustermann"),), aliases={})

    monkeypatch.setattr(calendar, "_roster", stale_roster)
    answer_view(fake, fixture_events("g2-series-first-read"))

    counts = await run(*G2_DAYS)

    assert (counts.occurrences, counts.lessons_created, counts.unassigned) == (3, 0, 3)
    event = await _event(session, G2_MOVED)
    assert (event.reason, event.title_name) == (NoLessonReason.UNMATCHED, "Max Mustermann")


# --- what makes a lesson touched ---


CORRECTIONS: dict[str, dict[str, Any]] = {
    "minutes": {"minutes": 45},
    "start": {"starts_at": dt.datetime(2026, 12, 1, 15, 30, tzinfo=dt.UTC)},
    "note": {"note": "Synthetic note"},
    "own-rate": {"rate_cents_per_hour": 4000},
}


@pytest.mark.parametrize("change", ["moved", "vanished"])
@pytest.mark.parametrize("correction", list(CORRECTIONS))
async def test_a_planned_lesson_corrected_by_hand_is_touched_and_never_follows_or_goes(
    session: AsyncSession, fake: FakeGraph, run, correction: str, change: str
):
    """Untouched means as the import made it: `planned`, no note, no own rate, the start and length of its
    occurrence as last seen. A hand correction is never reverted and never dropped with the lesson."""
    await new_student(session, MAX)
    first_read = fixture_events("g2-series-first-read")
    answer_view(fake, first_read)
    await run(*G2_DAYS)
    planned_id = (await _lesson_of(session, G2_MOVED)).id
    await lessons.update_lesson(session, planned_id, **CORRECTIONS[correction])
    row = await _row(session, planned_id)

    if change == "moved":
        answer_view(fake, fixture_events("g2-series-second-read"))
    else:
        answer_view(fake, [event for event in first_read if event["id"] != G2_MOVED])
    counts = await run(*G2_DAYS)

    assert await _row(session, planned_id) == row
    assert (counts.lessons_updated, counts.lessons_removed, counts.changed) == (0, 0, 1)


async def test_a_second_vanishing_is_flagged_again_after_the_occurrence_was_seen_in_between(
    session: AsyncSession, fake: FakeGraph, run
):
    """`vanished` is cleared when the occurrence is seen again, so its next vanishing is a difference of its own."""
    await new_student(session, MAX)
    first_read = fixture_events("g2-series-first-read")
    without = [event for event in first_read if event["id"] != G2_MOVED]
    answer_view(fake, first_read)
    await run(*G2_DAYS)
    await _tick(session, await _lesson_of(session, G2_MOVED))

    for read in (without, first_read):
        answer_view(fake, read)
        await run(*G2_DAYS)
        await calendar.dismiss_event(session, (await _event(session, G2_MOVED)).id)
    assert (await _event(session, G2_MOVED)).vanished is False

    answer_view(fake, without)
    await run(*G2_DAYS)

    event = await _event(session, G2_MOVED)
    assert (event.changed, event.vanished) == (True, True)


async def test_a_vanishing_after_a_dismissed_move_is_flagged_without_an_import_in_between(
    session: AsyncSession, fake: FakeGraph, run
):
    """A dismiss acknowledges what was seen; the vanishing seen later is a new difference."""
    await new_student(session, MAX)
    first_read = fixture_events("g2-series-first-read")
    answer_view(fake, first_read)
    await run(*G2_DAYS)
    await _tick(session, await _lesson_of(session, G2_MOVED))
    answer_view(fake, fixture_events("g2-series-second-read"))
    await run(*G2_DAYS)
    await calendar.dismiss_event(session, (await _event(session, G2_MOVED)).id)

    answer_view(fake, [event for event in first_read if event["id"] != G2_MOVED])
    await run(*G2_DAYS)

    event = await _event(session, G2_MOVED)
    assert (event.changed, event.vanished) == (True, True)


# --- the end of the range, and a title that cannot be stored ---


async def test_an_occurrence_at_the_berlin_midnight_after_the_range_belongs_to_the_next_day(
    session: AsyncSession, fake: FakeGraph, run
):
    """The range ends at the Berlin midnight after its last day, exclusive: that midnight's lesson never vanishes
    from the day before."""
    await new_student(session, MAX)
    midnight = dt.datetime(2026, 12, 2, tzinfo=BERLIN).astimezone(dt.UTC)
    answer_view(fake, [graph_event("midnight", "Mathe mit Max Mustermann", midnight)])
    assert (await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))).lessons_created == 1
    before = await snapshot(session)

    answer_view(fake, [])
    assert await run(dt.date(2026, 12, 1), dt.date(2026, 12, 1)) == ImportCounts()
    assert await snapshot(session) == before


async def test_a_title_with_a_nul_character_is_read_as_no_title(session: AsyncSession, fake: FakeGraph, run):
    await new_student(session, MAX)
    start = dt.datetime(2026, 12, 2, 15, tzinfo=dt.UTC)
    answer_view(fake, [graph_event("nul", "Mathe mit Max\x00Mustermann", start)])

    counts = await run(dt.date(2026, 12, 2), dt.date(2026, 12, 2))

    assert (counts.lessons_created, counts.unassigned) == (0, 1)
    event = await _event(session, "nul")
    assert (event.reason, event.title_subject, event.title_name) == (NoLessonReason.NO_TITLE_PATTERN, None, None)
