"""The lesson routes on Postgres: adding, reading, the life cycle of the tick, and the list by Berlin days."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import Company, Lesson, Party, PartyType, Person

pytestmark = pytest.mark.db

BERLIN = ZoneInfo("Europe/Berlin")
START = "2026-11-02T16:00:00+01:00"


async def _add(client: AsyncClient, student: uuid.UUID, **fields: Any) -> dict[str, Any]:
    response = await client.post(
        "/lessons", json={"student_id": str(student), "subject": "Mathe", "starts_at": START, "minutes": 60, **fields}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _patch(client: AsyncClient, lesson: dict[str, Any], **fields: Any):
    return await client.patch(f"/lessons/{lesson['id']}", json=fields)


async def _row(session: AsyncSession, lesson: dict[str, Any]) -> Lesson:
    row = await session.scalar(
        select(Lesson).where(Lesson.id == uuid.UUID(lesson["id"])).execution_options(populate_existing=True)
    )
    assert row is not None
    return row


def _error(response) -> str:
    return response.json()["code"]


# --- POST and GET ---


async def test_a_lesson_added_by_hand_starts_planned_and_not_billable(client: AsyncClient, student: uuid.UUID):
    response = await client.post(
        "/lessons",
        json={"student_id": str(student), "subject": " Mathe ", "starts_at": START, "minutes": 45, "note": "extra"},
    )

    assert response.status_code == 201
    lesson = response.json()
    assert lesson == {
        "id": lesson["id"],
        "student_id": str(student),
        "subject": "Mathe",
        "starts_at": "2026-11-02T15:00:00Z",
        "minutes": 45,
        "status": "planned",
        "billable": False,
        "rate_cents_per_hour": None,
        "note": "extra",
        "created_at": lesson["created_at"],
        "updated_at": lesson["updated_at"],
    }
    assert response.headers["location"] == f"/api/v1/billing/lessons/{lesson['id']}"
    assert (await client.get(f"/lessons/{lesson['id']}")).json() == lesson


async def test_reading_a_missing_lesson_is_lesson_not_found(client: AsyncClient):
    response = await client.get(f"/lessons/{uuid.uuid4()}")

    assert response.status_code == 404
    assert _error(response) == "lesson_not_found"


@pytest.mark.parametrize("minutes", [15, 24 * 60])
async def test_a_quarter_hour_and_a_whole_day_are_accepted_minutes(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID, minutes: int
):
    """Both bounds: 15-minute lessons are in the calendar (spec, "Verified behavior"), a day is the longest."""
    added = await _add(client, student, minutes=minutes)
    other = await _add(client, student, starts_at="2026-11-03T16:00:00+01:00")

    patched = await _patch(client, other, minutes=minutes)

    assert (added["minutes"], (await _row(session, added)).minutes) == (minutes, minutes)
    assert patched.status_code == 200
    assert (patched.json()["minutes"], (await _row(session, other)).minutes) == (minutes, minutes)


@pytest.mark.parametrize("minutes", [0, 20, 50, 61, 24 * 60 + 15])
async def test_minutes_off_the_quarter_hour_are_a_422(client: AsyncClient, student: uuid.UUID, minutes: int):
    response = await client.post(
        "/lessons", json={"student_id": str(student), "subject": "Mathe", "starts_at": START, "minutes": minutes}
    )

    assert response.status_code == 422
    assert _error(response) == "validation_error"
    assert response.json()["errors"][0]["loc"] == ["body", "minutes"]


@pytest.mark.parametrize("starts_at", ["2026-11-02T16:00:00", "0001-01-01T00:30:00+01:00"])
async def test_a_start_without_offset_or_without_a_utc_instant_is_a_422(
    client: AsyncClient, student: uuid.UUID, starts_at: str
):
    response = await client.post(
        "/lessons", json={"student_id": str(student), "subject": "Mathe", "starts_at": starts_at, "minutes": 60}
    )

    assert response.status_code == 422
    assert response.json()["errors"][0]["loc"] == ["body", "starts_at"]


async def test_a_lessons_own_rate_obeys_rule_g(client: AsyncClient, student: uuid.UUID):
    """PD3: cents per hour, 0 allowed, divisible by 4; the service answers before ck_lesson_quarter_hour_cents."""
    free = await _add(client, student, rate_cents_per_hour=0)
    off = await client.post(
        "/lessons",
        json={
            "student_id": str(student),
            "subject": "Mathe",
            "starts_at": "2026-11-03T16:00:00+01:00",
            "minutes": 60,
            "rate_cents_per_hour": 3002,
        },
    )
    negative = await _patch(client, free, rate_cents_per_hour=-4)
    patched_off = await _patch(client, free, rate_cents_per_hour=3001)

    assert free["rate_cents_per_hour"] == 0
    assert (off.status_code, _error(off)) == (422, "rate_not_quarter_hour")
    assert (patched_off.status_code, _error(patched_off)) == (422, "rate_not_quarter_hour")
    assert (negative.status_code, _error(negative)) == (422, "validation_error")
    assert (await _patch(client, free, rate_cents_per_hour=3200)).json()["rate_cents_per_hour"] == 3200
    assert (await _patch(client, free, rate_cents_per_hour=None)).json()["rate_cents_per_hour"] is None


async def _person_without_role(session: AsyncSession) -> uuid.UUID:
    party = Party(id=uuid.uuid4(), type=PartyType.PERSON, person=Person(firstname="Parent", lastname="A"))
    session.add(party)
    await session.flush()
    return party.id


async def _company(session: AsyncSession) -> uuid.UUID:
    party = Party(id=uuid.uuid4(), type=PartyType.COMPANY, company=Company(name="Company C"))
    session.add(party)
    await session.flush()
    return party.id


@pytest.mark.parametrize("make_party", [_person_without_role, _company, None], ids=["person", "company", "missing"])
async def test_a_student_id_that_is_no_student_is_unknown_student(
    client: AsyncClient,
    session: AsyncSession,
    student: uuid.UUID,
    make_party: Callable[[AsyncSession], Awaitable[uuid.UUID]] | None,
):
    other = await make_party(session) if make_party else uuid.uuid4()
    lesson = await _add(client, student)

    created = await client.post(
        "/lessons", json={"student_id": str(other), "subject": "Mathe", "starts_at": START, "minutes": 60}
    )
    moved = await _patch(client, lesson, student_id=str(other))

    assert (created.status_code, _error(created)) == (422, "unknown_student")
    assert (moved.status_code, _error(moved)) == (422, "unknown_student")
    assert str(other) not in created.text + moved.text
    assert (await _row(session, lesson)).student_party_id == student


async def test_a_lesson_moves_to_another_student(
    client: AsyncClient, student: uuid.UUID, new_student: Callable[[], Awaitable[uuid.UUID]]
):
    other = await new_student()
    lesson = await _add(client, student)

    response = await _patch(client, lesson, student_id=str(other))

    assert response.status_code == 200
    assert response.json()["student_id"] == str(other)


# --- uq_lesson_student_start ---


async def test_a_second_lesson_of_one_student_at_one_start_is_lesson_already_exists(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID, new_student: Callable[[], Awaitable[uuid.UUID]]
):
    await _add(client, student)

    # The same instant, written in UTC: the start is compared as an instant.
    response = await client.post(
        "/lessons",
        json={"student_id": str(student), "subject": "Physik", "starts_at": "2026-11-02T15:00:00Z", "minutes": 45},
    )

    assert response.status_code == 409
    assert _error(response) == "lesson_already_exists"
    # Another student at the same start is no conflict.
    await _add(client, await new_student())


async def test_moving_a_lesson_onto_another_of_the_same_student_is_lesson_already_exists(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID, new_student: Callable[[], Awaitable[uuid.UUID]]
):
    await _add(client, student)
    later = await _add(client, student, starts_at="2026-11-02T17:00:00+01:00")
    other = await new_student()
    elsewhere = await _add(client, other)

    moved = await _patch(client, later, starts_at=START)
    switched = await _patch(client, elsewhere, student_id=str(student))

    assert (moved.status_code, _error(moved)) == (409, "lesson_already_exists")
    assert (switched.status_code, _error(switched)) == (409, "lesson_already_exists")
    assert (await _row(session, later)).starts_at == datetime(2026, 11, 2, 16, tzinfo=UTC)


# --- the life cycle (billing spec, "A lesson's life"; decision M) ---


async def test_the_tick_sets_held_and_billable(client: AsyncClient, session: AsyncSession, student: uuid.UUID):
    lesson = await _add(client, student)

    response = await _patch(client, lesson, status="held")

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["billable"]) == ("held", True)
    row = await _row(session, lesson)
    assert (row.status, row.billable) == ("held", True)


@pytest.mark.parametrize("status", ["planned", "not_held"])
async def test_billable_on_a_planned_or_not_held_lesson_is_lesson_not_billable(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID, status: str
):
    lesson = await _add(client, student)
    await _patch(client, lesson, status=status)

    alone = await _patch(client, lesson, billable=True)
    together = await _patch(client, lesson, status=status, billable=True)

    assert (alone.status_code, _error(alone)) == (422, "lesson_not_billable")
    assert (together.status_code, _error(together)) == (422, "lesson_not_billable")
    row = await _row(session, lesson)
    assert (row.status, row.billable) == (status, False)


@pytest.mark.parametrize("status", ["planned", "not_held"])
@pytest.mark.parametrize("billable_from", ["held", "cancelled"])
async def test_moving_a_billable_lesson_to_planned_or_not_held_clears_the_flag(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID, billable_from: str, status: str
):
    lesson = await _add(client, student)
    assert (await _patch(client, lesson, status=billable_from, billable=True)).json()["billable"] is True

    response = await _patch(client, lesson, status=status)

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["billable"]) == (status, False)
    row = await _row(session, lesson)
    assert (row.status, row.billable) == (status, False)


async def test_moving_a_billable_lesson_to_planned_with_billable_false_is_allowed(
    client: AsyncClient, student: uuid.UUID
):
    lesson = await _add(client, student)
    await _patch(client, lesson, status="held")

    response = await _patch(client, lesson, status="planned", billable=False)

    assert (response.json()["status"], response.json()["billable"]) == ("planned", False)


async def test_a_cancelled_lesson_is_billable_only_after_the_maintainer_said_so(
    client: AsyncClient, student: uuid.UUID
):
    lesson = await _add(client, student)

    cancelled = await _patch(client, lesson, status="cancelled")
    billed = await _patch(client, lesson, billable=True)
    off_again = await _patch(client, lesson, billable=False)

    assert (cancelled.json()["status"], cancelled.json()["billable"]) == ("cancelled", False)
    assert (billed.json()["status"], billed.json()["billable"]) == ("cancelled", True)
    assert off_again.json()["billable"] is False


async def test_a_billable_lesson_moved_to_cancelled_is_no_longer_billable_by_itself(
    client: AsyncClient, student: uuid.UUID
):
    """Ruling: entering a status sets its default - a late cancellation is never billable by itself (decision M)."""
    lesson = await _add(client, student)
    await _patch(client, lesson, status="held")

    cancelled = await _patch(client, lesson, status="cancelled")
    cancelled_and_billed = await _patch(client, lesson, status="held")
    both = await _patch(client, lesson, status="cancelled", billable=True)

    assert (cancelled.json()["status"], cancelled.json()["billable"]) == ("cancelled", False)
    assert cancelled_and_billed.json()["billable"] is True
    assert (both.json()["status"], both.json()["billable"]) == ("cancelled", True)


async def test_goodwill_switches_billable_off_on_a_held_lesson_and_a_repeated_tick_keeps_it(
    client: AsyncClient, student: uuid.UUID
):
    """Ruling: sending the status a lesson already has enters nothing - a repeated tick does not undo goodwill."""
    lesson = await _add(client, student)
    await _patch(client, lesson, status="held")

    goodwill = await _patch(client, lesson, billable=False)
    repeated = await _patch(client, lesson, status="held")
    billed_again = await _patch(client, lesson, billable=True)

    assert (goodwill.json()["status"], goodwill.json()["billable"]) == ("held", False)
    assert (repeated.json()["status"], repeated.json()["billable"]) == ("held", False)
    assert billed_again.json()["billable"] is True


async def test_a_tick_with_billable_false_is_goodwill_in_one_step(client: AsyncClient, student: uuid.UUID):
    lesson = await _add(client, student)

    response = await _patch(client, lesson, status="held", billable=False)

    assert (response.json()["status"], response.json()["billable"]) == ("held", False)


async def test_a_patch_corrects_the_lessons_data_and_an_empty_one_changes_nothing(
    client: AsyncClient, student: uuid.UUID
):
    lesson = await _add(client, student, note="first")

    empty = await _patch(client, lesson)
    corrected = await _patch(
        client, lesson, subject="Physik", starts_at="2026-11-02T17:15:00+01:00", minutes=90, note=None
    )

    assert empty.status_code == 200
    assert empty.json() == lesson
    assert corrected.json() == {
        **lesson,
        "subject": "Physik",
        "starts_at": "2026-11-02T16:15:00Z",
        "minutes": 90,
        "note": None,
        "updated_at": corrected.json()["updated_at"],
    }


@pytest.mark.parametrize(
    "fields",
    [{"status": None}, {"billable": None}, {"minutes": None}, {"subject": ""}, {"minutes": 20}, {"status": "done"}],
)
async def test_a_patch_refuses_what_the_request_model_refuses(
    client: AsyncClient, student: uuid.UUID, fields: dict[str, Any]
):
    lesson = await _add(client, student)

    response = await _patch(client, lesson, **fields)

    assert (response.status_code, _error(response)) == (422, "validation_error")


async def test_patching_a_missing_lesson_is_lesson_not_found(client: AsyncClient):
    response = await client.patch(f"/lessons/{uuid.uuid4()}", json={"status": "held"})

    assert (response.status_code, _error(response)) == (404, "lesson_not_found")


# --- the list ---


async def _ids(client: AsyncClient, **params: Any) -> list[str]:
    response = await client.get("/lessons", params=params)
    assert response.status_code == 200, response.text
    return [lesson["id"] for lesson in response.json()["items"]]


async def test_the_list_pages_by_start_and_filters_by_student_and_status(
    client: AsyncClient, student: uuid.UUID, new_student: Callable[[], Awaitable[uuid.UUID]]
):
    other = await new_student()
    late = await _add(client, student, starts_at="2026-11-04T16:00:00+01:00")
    early = await _add(client, student, starts_at="2026-11-03T16:00:00+01:00")
    others = await _add(client, other)
    await _patch(client, late, status="held")

    page = (await client.get("/lessons", params={"limit": 2, "offset": 1})).json()

    assert await _ids(client) == [others["id"], early["id"], late["id"]]
    assert (page["total"], [item["id"] for item in page["items"]]) == (3, [early["id"], late["id"]])
    assert await _ids(client, student_id=str(student)) == [early["id"], late["id"]]
    assert await _ids(client, status="held") == [late["id"]]
    assert await _ids(client, student_id=str(uuid.uuid4())) == []
    assert await _ids(client, **{"from": "2026-11-05", "to": "2026-11-01"}) == []


@pytest.mark.parametrize("params", [{"from": "0001-01-01"}, {"to": "9999-12-31"}, {"from_": "2026-11-01"}])
async def test_the_list_refuses_days_without_a_utc_midnight_and_unknown_parameters(
    client: AsyncClient, params: dict[str, str]
):
    response = await client.get("/lessons", params=params)

    assert (response.status_code, _error(response)) == (422, "validation_error")


def _berlin(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=BERLIN)


# (start in Berlin, its Berlin day): late evenings and the first minutes after midnight, whose UTC day differs.
BERLIN_DAYS = {
    "summer-late-evening": (_berlin(2026, 7, 15, 23, 45), date(2026, 7, 15)),
    "summer-midnight": (_berlin(2026, 7, 16, 0, 0), date(2026, 7, 16)),
    "winter-late-evening": (_berlin(2026, 1, 15, 23, 45), date(2026, 1, 15)),
    "winter-midnight": (_berlin(2026, 1, 16, 0, 0), date(2026, 1, 16)),
    # 29 March 2026: 02:00 CET becomes 03:00 CEST - the night before in winter time, the evening after in summer time.
    "spring-eve-late-evening": (_berlin(2026, 3, 28, 23, 45), date(2026, 3, 28)),
    "spring-change-midnight": (_berlin(2026, 3, 29, 0, 0), date(2026, 3, 29)),
    "spring-change-late-evening": (_berlin(2026, 3, 29, 23, 45), date(2026, 3, 29)),
    "spring-after-midnight": (_berlin(2026, 3, 30, 0, 0), date(2026, 3, 30)),
    # 25 October 2026: 03:00 CEST becomes 02:00 CET - the night before in summer time, the evening after in winter time.
    "autumn-eve-late-evening": (_berlin(2026, 10, 24, 23, 45), date(2026, 10, 24)),
    "autumn-change-midnight": (_berlin(2026, 10, 25, 0, 0), date(2026, 10, 25)),
    "autumn-change-late-evening": (_berlin(2026, 10, 25, 23, 45), date(2026, 10, 25)),
    "autumn-after-midnight": (_berlin(2026, 10, 26, 0, 0), date(2026, 10, 26)),
}


@pytest.mark.parametrize("hours", [0, 1, 2], ids=["utc", "fixed-cet", "fixed-cest"])
def test_the_berlin_day_cases_catch_a_filter_in_utc_or_in_a_fixed_offset(hours: int):
    """The cases are only worth something if a filter by UTC days, or by a fixed offset, misfiles one of them."""
    wrong = timezone(timedelta(hours=hours))

    assert [name for name, (start, day) in BERLIN_DAYS.items() if start.astimezone(wrong).date() != day]


@pytest.mark.parametrize(("starts_at", "day"), BERLIN_DAYS.values(), ids=BERLIN_DAYS.keys())
async def test_the_list_filters_by_berlin_days_both_included(
    client: AsyncClient, student: uuid.UUID, starts_at: datetime, day: date
):
    # Sent in UTC: the day comes from the instant, not from the offset the client wrote.
    lesson = await _add(client, student, starts_at=starts_at.astimezone(UTC).isoformat())
    before, after = (day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat()

    assert await _ids(client, **{"from": day.isoformat(), "to": day.isoformat()}) == [lesson["id"]]
    assert await _ids(client, **{"from": before, "to": after}) == [lesson["id"]]
    assert await _ids(client, to=before) == []
    assert await _ids(client, **{"from": after}) == []
