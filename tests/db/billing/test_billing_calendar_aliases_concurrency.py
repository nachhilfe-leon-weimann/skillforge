"""Calendar aliases across real transactions: an overlapping write answers by a service rule, never as a 500.

These tests commit, so they cannot run on the rolled-back ``session`` fixture; they remove what they created.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, func, select

from app.core.db import Database
from app.core.db.models import CalendarAlias, Party, PreferredMeetingTool
from app.services.billing import calendar_aliases
from app.services.billing.errors import CalendarAliasAlreadyExistsError, UnknownStudentError
from app.services.crm import parties, persons
from app.services.crm.inputs import StudentRoleData

pytestmark = pytest.mark.db


@pytest.fixture
async def students(db: Database) -> AsyncIterator[list[uuid.UUID]]:
    """Two committed synthetic students; afterwards their aliases and whatever is left of them are removed."""
    async with db.session() as setup:
        created = [
            await persons.create_person(
                setup,
                firstname="Race",
                lastname=lastname,
                student=StudentRoleData(preferred_meeting_tool=PreferredMeetingTool.DISCORD),
            )
            for lastname in ("E", "F")
        ]
    ids = [party.id for party in created]
    try:
        yield ids
    finally:
        async with db.session() as cleanup:
            await cleanup.execute(delete(CalendarAlias).where(CalendarAlias.student_party_id.in_(ids)))
            await cleanup.execute(delete(Party).where(Party.id.in_(ids)))


async def test_an_overlapping_alias_for_the_same_name_waits_and_then_is_calendar_alias_already_exists(
    db: Database, students: list[uuid.UUID]
):
    first, second = db.session_factory(), db.session_factory()
    try:
        # The first request has stored the alias but not committed yet ...
        await calendar_aliases.create_alias(first, name="Race Student", student_id=students[0])
        # ... when another spelling of the name arrives for the other student: it waits on the unique index.
        pending = asyncio.create_task(
            calendar_aliases.create_alias(second, name="RACE-student", student_id=students[1])
        )
        await asyncio.sleep(0.5)
        assert not pending.done(), "the second alias waits for the first"

        await first.commit()
        with pytest.raises(CalendarAliasAlreadyExistsError) as conflict:
            await asyncio.wait_for(pending, timeout=10)
        assert "race" not in f"{conflict.value} {conflict.value!r}".casefold()
    finally:
        await first.close()
        await second.close()

    async with db.session(write=False) as check:
        stored = await check.scalar(
            select(func.count()).select_from(CalendarAlias).where(CalendarAlias.student_party_id.in_(students))
        )
    assert stored == 1


async def test_an_alias_naming_a_student_whose_delete_is_under_way_waits_and_then_is_unknown_student(
    db: Database, students: list[uuid.UUID]
):
    """The alias waits for the delete and finds no party: `unknown_student`, not the foreign key's 500."""
    deleted = students[1]
    deleting, writing = db.session_factory(), db.session_factory()
    try:
        # The delete has removed the party but not committed yet ...
        await parties.delete_party(deleting, deleted)
        # ... when the alias arrives: it waits on the party's lock instead of reading the party it still sees.
        pending = asyncio.create_task(calendar_aliases.create_alias(writing, name="Race F", student_id=deleted))
        await asyncio.sleep(0.5)
        assert not pending.done(), "the alias waits for the delete"

        await deleting.commit()
        with pytest.raises(UnknownStudentError):
            await asyncio.wait_for(pending, timeout=10)
    finally:
        await deleting.close()
        await writing.close()
