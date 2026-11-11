"""The calendar alias routes (billing spec, SF4-2), on Postgres. Every name is synthetic."""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import ANY

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import CalendarAlias
from app.services.crm import companies, persons

pytestmark = pytest.mark.db

ALIASES = "/calendar/aliases"


async def _create(client: AsyncClient, name: str, student: uuid.UUID) -> dict[str, Any]:
    response = await client.post(ALIASES, json={"name": name, "student_id": str(student)})
    assert response.status_code == 201, response.text
    return response.json()


async def _count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(CalendarAlias)) or 0


async def test_an_alias_is_stored_normalised_and_listed(client: AsyncClient, student: uuid.UUID):
    created = await _create(client, "  Jürgen   MÜLLER-Beispiel_B ", student)

    assert created == {
        "id": ANY,
        "name_normalized": "juergen mueller beispiel b",
        "student_id": str(student),
        "created_at": ANY,
    }
    listed = (await client.get(ALIASES)).json()
    assert listed["items"] == [created]
    assert listed["total"] == 1


async def test_the_list_is_a_page_by_name(
    client: AsyncClient, new_student: Callable[[], Awaitable[uuid.UUID]], student: uuid.UUID
):
    other = await new_student()
    for name, owner in (("Student C", student), ("Student A", other), ("Student B", student)):
        await _create(client, name, owner)

    page = (await client.get(ALIASES, params={"limit": 2, "offset": 1})).json()

    assert [alias["name_normalized"] for alias in page["items"]] == ["student b", "student c"]
    assert (page["total"], page["limit"], page["offset"]) == (3, 2, 1)


@pytest.mark.parametrize("spelling", ["Student B", "student-b", "STUDENT  B", "Student_B"])
async def test_a_name_that_normalises_like_a_stored_one_is_calendar_alias_already_exists(
    client: AsyncClient,
    session: AsyncSession,
    new_student: Callable[[], Awaitable[uuid.UUID]],
    student: uuid.UUID,
    spelling: str,
):
    """For the same student and for another one: one name maps to one student."""
    await _create(client, "Student B", student)

    for owner in (student, await new_student()):
        response = await client.post(ALIASES, json={"name": spelling, "student_id": str(owner)})

        assert response.status_code == 409
        assert response.json() == {
            "detail": "An alias for this calendar name already exists",
            "code": "calendar_alias_already_exists",
        }
    assert await _count(session) == 1


async def test_a_party_that_is_no_student_is_unknown_student(client: AsyncClient, session: AsyncSession):
    person = await persons.create_person(session, firstname="Parent", lastname="A")
    company = await companies.create_company(session, name="Company A")

    for party_id in (person.id, company.id, uuid.uuid4()):
        response = await client.post(ALIASES, json={"name": "Student B", "student_id": str(party_id)})

        assert response.status_code == 422
        assert response.json() == {"detail": "Unknown student", "code": "unknown_student"}
    assert await _count(session) == 0


@pytest.mark.parametrize("name", ["", "   ", "-_-", " - ", "Student\x00B"])
async def test_a_name_with_nothing_to_match_is_refused_without_repeating_it(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID, name: str
):
    response = await client.post(ALIASES, json={"name": name, "student_id": str(student)})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert "Student" not in response.text
    assert await _count(session) == 0


async def test_an_alias_is_deleted_once(client: AsyncClient, session: AsyncSession, student: uuid.UUID):
    alias = await _create(client, "Student B", student)

    deleted = await client.delete(f"{ALIASES}/{alias['id']}")
    again = await client.delete(f"{ALIASES}/{alias['id']}")

    assert deleted.status_code == 204
    assert again.status_code == 404
    assert again.json() == {"detail": "Calendar alias not found", "code": "calendar_alias_not_found"}
    assert await _count(session) == 0
    # The name is free again.
    await _create(client, "Student B", student)
