"""The calendar import (billing spec, SF4-3), on Postgres, with Graph behind a mocked transport over G2 to G5.

The calendar proposes, a person confirms: an untouched `planned` lesson follows its appointment, a lesson that is not
`planned` never changes - its occurrence is flagged `changed` - and only an occurrence that starts inside the
imported range can vanish. Every name is synthetic.
"""

import datetime as dt
import uuid
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import CalendarEvent, Lesson, LessonStatus
from app.services.billing import calendar, calendar_aliases, lessons
from app.services.billing.calendar import ImportCounts
from app.services.billing.calendar_rules import NoLessonReason
from app.services.billing.errors import CalendarUnavailableError
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


async def test_an_occurrence_with_a_lesson_keeps_its_student_when_the_match_changes(
    session: AsyncSession, fake: FakeGraph, run
):
    await new_student(session, MAX)
    other = await new_student(session, ("Moritz", "Beispiel"))
    answer_view(fake, fixture_events("g2-series-first-read"))
    await run(*G2_DAYS)
    before = await snapshot(session)

    await calendar_aliases.create_alias(session, name="Max Mustermann", student_id=other)
    await run(*G2_DAYS)

    assert await snapshot(session) == before


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
    await new_student(session, MAX)
    answer_view(fake, fixture_events("g2-series-first-read"))
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
