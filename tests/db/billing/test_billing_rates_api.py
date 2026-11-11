"""The rate routes on Postgres: a student's own rate from a day on, set idempotently, listed newest first."""

import uuid
from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import Lesson, Rate
from app.services.crm import persons, roles

pytestmark = pytest.mark.db

DAY = "2026-09-01"


def _rates(student: uuid.UUID) -> str:
    return f"/students/{student}/rates"


async def _put(client: AsyncClient, student: uuid.UUID, day: str = DAY, **body: Any):
    return await client.put(f"{_rates(student)}/{day}", json={"cents_per_hour": 3200, **body})


async def _row_version(session: AsyncSession, student: uuid.UUID, day: str = DAY) -> str | None:
    """The physical place of the row: any UPDATE writes a new version elsewhere, even within one transaction, where
    ``now()`` - and so ``updated_at`` - stands still."""
    return await session.scalar(
        text("SELECT ctid::text FROM billing.rate WHERE student_party_id = :student AND valid_from = :day"),
        {"student": student, "day": date.fromisoformat(day)},
    )


async def _count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Rate)) or 0


def _error(response) -> str:
    return response.json()["code"]


async def test_put_sets_a_rate_from_its_day_on(client: AsyncClient, student: uuid.UUID):
    response = await _put(client, student, note="from the new school year")

    assert response.status_code == 200
    rate = response.json()
    assert rate == {
        "student_id": str(student),
        "valid_from": DAY,
        "cents_per_hour": 3200,
        "note": "from the new school year",
        "created_at": rate["created_at"],
        "updated_at": rate["updated_at"],
    }


async def test_setting_a_rate_twice_for_the_same_day_changes_nothing(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID, new_student
):
    """A sibling's rate on the same day, set first, is neither the answer nor touched."""
    sibling = await new_student()
    await _put(client, sibling, cents_per_hour=2800)
    sibling_version = await _row_version(session, sibling)

    first = await _put(client, student, note="a note")
    version = await _row_version(session, student)
    second = await _put(client, student, note="a note")

    assert second.status_code == 200
    assert second.json() == first.json()
    assert (second.json()["student_id"], second.json()["cents_per_hour"]) == (str(student), 3200)
    assert await _row_version(session, student) == version
    assert await _row_version(session, sibling) == sibling_version
    assert await _count(session) == 2


async def test_put_on_a_day_already_set_replaces_amount_and_note(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID
):
    first = (await _put(client, student, note="a note")).json()
    version = await _row_version(session, student)
    changed = await _put(client, student, cents_per_hour=3600)

    assert changed.status_code == 200
    assert changed.json() == {**first, "cents_per_hour": 3600, "note": None, "updated_at": changed.json()["updated_at"]}
    assert await _row_version(session, student) != version
    assert await _count(session) == 1


async def test_a_note_alone_changes_the_rate(client: AsyncClient, session: AsyncSession, student: uuid.UUID):
    await _put(client, student)
    version = await _row_version(session, student)

    assert (await _put(client, student, note="agreed by mail")).json()["note"] == "agreed by mail"
    assert await _row_version(session, student) != version


@pytest.mark.parametrize("cents_per_hour", [3002, 3001, 1])
async def test_a_rate_that_breaks_rule_g_is_rate_not_quarter_hour(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID, cents_per_hour: int
):
    response = await _put(client, student, cents_per_hour=cents_per_hour)

    assert response.status_code == 422
    assert _error(response) == "rate_not_quarter_hour"
    assert await _count(session) == 0


@pytest.mark.parametrize("cents_per_hour", [0, 4, 2996, 3204])
async def test_a_quarter_hour_rate_is_accepted_and_stored(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID, cents_per_hour: int
):
    """Rule G from the accepting side: divisible by 4 is enough, whatever the hundreds say."""
    response = await _put(client, student, cents_per_hour=cents_per_hour)

    assert response.status_code == 200
    assert response.json()["cents_per_hour"] == cents_per_hour
    assert await session.scalar(select(Rate.cents_per_hour).where(Rate.student_party_id == student)) == cents_per_hour


@pytest.mark.parametrize("cents_per_hour", [4, 2996, 3204])
async def test_a_quarter_hour_rate_is_accepted_as_a_lessons_own_rate(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID, cents_per_hour: int
):
    """The same rule G (`require_quarter_hour_rate`) serves a lesson's own rate (PD3), on add and on change."""
    lesson = {"student_id": str(student), "subject": "Mathe", "minutes": 60}
    added = await client.post(
        "/lessons", json={**lesson, "starts_at": "2026-11-02T16:00:00+01:00", "rate_cents_per_hour": cents_per_hour}
    )
    other = (await client.post("/lessons", json={**lesson, "starts_at": "2026-11-03T16:00:00+01:00"})).json()
    patched = await client.patch(f"/lessons/{other['id']}", json={"rate_cents_per_hour": cents_per_hour})

    assert (added.status_code, added.json()["rate_cents_per_hour"]) == (201, cents_per_hour)
    assert (patched.status_code, patched.json()["rate_cents_per_hour"]) == (200, cents_per_hour)
    stored = await session.scalars(
        select(Lesson.rate_cents_per_hour)
        .where(Lesson.student_party_id == student)
        .execution_options(populate_existing=True)
    )
    assert list(stored) == [cents_per_hour, cents_per_hour]


async def test_a_rate_of_zero_is_a_quarter_hour_rate_and_a_negative_one_is_no_rate(
    client: AsyncClient, student: uuid.UUID
):
    free = await _put(client, student, cents_per_hour=0)
    negative = await _put(client, student, "2026-10-01", cents_per_hour=-4)

    assert free.status_code == 200
    assert negative.status_code == 422
    assert negative.json()["errors"][0]["loc"] == ["body", "cents_per_hour"]


async def test_the_list_holds_the_students_rates_newest_day_first(client: AsyncClient, student: uuid.UUID, new_student):
    sibling = await new_student()
    for day, cents in (("2026-01-01", 2800), ("2026-09-01", 3000), ("2026-04-01", 2900)):
        await _put(client, student, day, cents_per_hour=cents)
    await _put(client, sibling, "2026-05-01")

    page = (await client.get(_rates(student), params={"limit": 2})).json()
    rest = (await client.get(_rates(student), params={"limit": 2, "offset": 2})).json()

    assert [(rate["valid_from"], rate["cents_per_hour"]) for rate in page["items"] + rest["items"]] == [
        ("2026-09-01", 3000),
        ("2026-04-01", 2900),
        ("2026-01-01", 2800),
    ]
    assert (page["total"], page["limit"], page["offset"]) == (3, 2, 0)


async def test_delete_removes_the_rate_of_that_day_only(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID, new_student
):
    """Of that student only: a sibling's rate on the same day keeps the sibling's price."""
    sibling = await new_student()
    await _put(client, sibling, cents_per_hour=2800)
    await _put(client, student, "2026-01-01")
    await _put(client, student)

    deleted = await client.delete(f"{_rates(student)}/{DAY}")
    again = await client.delete(f"{_rates(student)}/{DAY}")

    assert deleted.status_code == 204
    assert again.status_code == 404
    assert _error(again) == "rate_not_found"
    assert [rate["valid_from"] for rate in (await client.get(_rates(student))).json()["items"]] == ["2026-01-01"]
    assert [
        (rate["valid_from"], rate["cents_per_hour"]) for rate in (await client.get(_rates(sibling))).json()["items"]
    ] == [(DAY, 2800)]
    assert await _count(session) == 2


@pytest.mark.parametrize(
    ("method", "suffix"), [("GET", ""), ("PUT", f"/{DAY}"), ("DELETE", f"/{DAY}")], ids=["get", "put", "delete"]
)
async def test_a_party_that_does_not_exist_is_party_not_found(client: AsyncClient, method: str, suffix: str):
    response = await client.request(method, f"{_rates(uuid.uuid4())}{suffix}", json={"cents_per_hour": 3200})

    assert response.status_code == 404
    assert _error(response) == "party_not_found"


async def test_a_party_without_the_student_role_gets_no_rate(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID
):
    parent = await persons.create_person(session, firstname="Parent", lastname="A")

    refused = await _put(client, parent.id)
    listed = await client.get(_rates(parent.id))

    assert refused.status_code == 422
    assert _error(refused) == "unknown_student"
    assert listed.status_code == 200
    assert listed.json()["items"] == []
    assert await _count(session) == 0


async def test_a_former_students_rates_stay_readable_and_removable(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID
):
    """A rate references the party, not the role (PD9): taking the role away must not strand a rate that keeps the
    party from being deleted."""
    await _put(client, student)
    await roles.remove_student_role(session, student)

    listed = await client.get(_rates(student))
    deleted = await client.delete(f"{_rates(student)}/{DAY}")

    assert [rate["valid_from"] for rate in listed.json()["items"]] == [DAY]
    assert deleted.status_code == 204
    assert await _count(session) == 0
