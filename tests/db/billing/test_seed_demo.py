"""`just seed-demo` on Postgres: synthetic data only, everything the screens need, and a second run changes no row."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import seed_demo
from app.core.db.models import ContactInfo, Lesson, LessonStatus, PartyRelation, PartyRelationType, Person, Rate
from app.core.db.models.base import Base

pytestmark = pytest.mark.db

TODAY = date(2026, 11, 11)


async def _snapshot(session: AsyncSession) -> dict[str, list[tuple[Any, ...]]]:
    """Every visible row of every table with its physical place: an UPDATE moves a row even within one transaction,
    where ``now()`` - and so every ``updated_at`` - stands still."""
    snapshot = {}
    for table in Base.metadata.sorted_tables:
        rows = await session.execute(text(f"SELECT ctid::text AS ctid, * FROM {table.fullname} ORDER BY ctid"))
        snapshot[table.fullname] = [tuple(row) for row in rows]
    return snapshot


async def _parties(session: AsyncSession) -> dict[str, Any]:
    """The seeded persons by the letter of their last name."""
    seeded = select(ContactInfo.party_id).where(ContactInfo.value.like("%@demo.example.org"))
    people = await session.scalars(select(Person).where(Person.party_id.in_(seeded)))
    return {person.lastname: person for person in people}


async def test_a_second_run_changes_no_row(session: AsyncSession):
    first = await seed_demo.seed(session, today=TODAY)
    before = await _snapshot(session)
    second = await seed_demo.seed(session, today=TODAY)

    assert (first.persons_created, first.lessons_created) == (len(seed_demo.PERSONS), len(seed_demo.LESSONS))
    assert (second.persons_created, second.lessons_created) == (0, 0)
    assert await _snapshot(session) == before
    assert sum(map(len, before.values())) > 0


async def test_the_demo_set_is_synthetic(session: AsyncSession):
    await seed_demo.seed(session, today=TODAY)
    people = await _parties(session)

    assert {(person.firstname, person.lastname) for person in people.values()} == {
        ("Parent", "A"),
        ("Student", "B"),
        ("Student", "C"),
        ("Student", "D"),
    }
    addresses = await session.scalars(
        select(ContactInfo.value).where(ContactInfo.party_id.in_(person.party_id for person in people.values()))
    )
    assert all(address.endswith("@demo.example.org") for address in addresses)
    notes = [rate.note for rate in seed_demo.RATES if rate.note]
    assert all(note.startswith("Demo:") for note in notes)


async def test_the_demo_set_has_what_the_screens_need(session: AsyncSession):
    await seed_demo.seed(session, today=TODAY)
    people = await _parties(session)
    ids = {letter: person.party_id for letter, person in people.items()}
    lessons = list(await session.scalars(select(Lesson).where(Lesson.student_party_id.in_(ids.values()))))
    paid_for = set(
        await session.scalars(
            select(PartyRelation.to_party_id).where(
                PartyRelation.from_party_id == ids["A"], PartyRelation.type == PartyRelationType.PAYS_FOR
            )
        )
    )
    payers_of_d = await session.scalars(
        select(PartyRelation.from_party_id).where(
            PartyRelation.to_party_id == ids["D"], PartyRelation.type == PartyRelationType.PAYS_FOR
        )
    )

    # Every status, and both sides of `billable` where the maintainer decides it.
    assert {lesson.status for lesson in lessons} == set(LessonStatus)
    assert {(lesson.status, lesson.billable) for lesson in lessons} >= {
        (LessonStatus.HELD, True),
        (LessonStatus.HELD, False),
        (LessonStatus.CANCELLED, True),
        (LessonStatus.CANCELLED, False),
    }
    assert any(lesson.rate_cents_per_hour is not None for lesson in lessons)
    # Ended lessons in the month before, planned ones in the current month.
    assert {lesson.starts_at.date().replace(day=1) for lesson in lessons} == {date(2026, 10, 1), date(2026, 11, 1)}
    # Family A pays for two siblings with different rates today; student D pays for themselves at the default rate.
    assert paid_for == {ids["B"], ids["C"]}
    assert await _rate_on(session, ids["B"], TODAY) != await _rate_on(session, ids["C"], TODAY)
    assert list(payers_of_d) == []
    assert await _rate_on(session, ids["D"], TODAY) is None


async def test_the_command_seeds_through_the_checked_url_and_prints_counts_only(
    session: AsyncSession, monkeypatch, capsys
):
    url = "postgresql+asyncpg://demo:secret-pw-4711@db:5432/skillforge"
    opened: list[str] = []

    class TestDatabase:
        """Stands in for the configured database: the test's session, in a SAVEPOINT as the command's transaction."""

        @classmethod
        def from_url(cls, given: str) -> TestDatabase:
            opened.append(given)
            return cls()

        @asynccontextmanager
        async def session(self) -> AsyncGenerator[AsyncSession]:
            async with session.begin_nested():
                yield session

        async def dispose(self) -> None:
            pass

    monkeypatch.setattr(seed_demo, "get_settings", lambda: SimpleNamespace(db=SimpleNamespace(url=url)))
    monkeypatch.setattr(seed_demo, "Database", TestDatabase)

    await seed_demo.seed_demo(today=TODAY)
    await seed_demo.seed_demo(today=TODAY)

    assert opened == [url, url]
    first, second = capsys.readouterr().out.split("database host: ")[1:]
    assert first.splitlines() == [
        "single-label host name",
        "persons: 4 created, 0 already there",
        "relations: 4 in place",
        "rates: 3 in place",
        "lessons: 11 created, 0 already there",
    ]
    assert second.splitlines()[1::3] == ["persons: 0 created, 4 already there", "lessons: 0 created, 11 already there"]
    assert "secret-pw-4711" not in first + second


async def _rate_on(session: AsyncSession, student_id, day: date) -> int | None:
    return await session.scalar(
        select(Rate.cents_per_hour)
        .where(Rate.student_party_id == student_id, Rate.valid_from <= day)
        .order_by(Rate.valid_from.desc())
        .limit(1)
    )
