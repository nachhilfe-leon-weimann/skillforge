"""Rates across real transactions: what one transaction cannot show, since ``now()`` stands still inside it.

These tests commit, so they cannot run on the rolled-back ``session`` fixture; they remove what they created.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import date

import pytest
from sqlalchemy import delete, select

from app.core.db import Database
from app.core.db.models import Party, PreferredMeetingTool, Rate
from app.services.billing import rates
from app.services.crm import parties, persons
from app.services.crm.errors import PartyNotFoundError
from app.services.crm.inputs import StudentRoleData

pytestmark = pytest.mark.db

DAY = date(2026, 9, 1)


@pytest.fixture
async def students(db: Database) -> AsyncIterator[list[uuid.UUID]]:
    """Two committed synthetic students; afterwards their rates and whatever is left of them are removed."""
    async with db.session() as setup:
        created = [
            await persons.create_person(
                setup,
                firstname="Race",
                lastname=lastname,
                student=StudentRoleData(preferred_meeting_tool=PreferredMeetingTool.DISCORD),
            )
            for lastname in ("C", "D")
        ]
    ids = [party.id for party in created]
    try:
        yield ids
    finally:
        async with db.session() as cleanup:
            await cleanup.execute(delete(Rate).where(Rate.student_party_id.in_(ids)))
            await cleanup.execute(delete(Party).where(Party.id.in_(ids)))


async def _put(db: Database, student: uuid.UUID, cents_per_hour: int) -> None:
    async with db.session() as session:
        await rates.put_rate(session, student, DAY, cents_per_hour=cents_per_hour)


async def _updated_at(db: Database, student: uuid.UUID):
    async with db.session(write=False) as session:
        return await session.scalar(
            select(Rate.updated_at).where(Rate.student_party_id == student, Rate.valid_from == DAY)
        )


async def test_updated_at_moves_on_a_replace_and_stays_on_an_identical_put(db: Database, students: list[uuid.UUID]):
    student = students[0]
    await _put(db, student, 3200)
    set_at = await _updated_at(db, student)

    await _put(db, student, 3200)
    unchanged_at = await _updated_at(db, student)
    await _put(db, student, 3600)
    replaced_at = await _updated_at(db, student)

    assert unchanged_at == set_at
    assert replaced_at is not None and set_at is not None and replaced_at > set_at


async def test_a_put_for_a_student_whose_delete_is_under_way_waits_and_then_is_party_not_found(
    db: Database, students: list[uuid.UUID]
):
    """The rate's party is in the path: it waits for the delete and finds no party - `party_not_found`, not the
    foreign key's 500."""
    deleted = students[1]
    deleting, writing = db.session_factory(), db.session_factory()
    try:
        # The delete has removed the party but not committed yet ...
        await parties.delete_party(deleting, deleted)
        # ... when the rate arrives: it waits on the party's lock instead of reading the party it still sees.
        pending = asyncio.create_task(rates.put_rate(writing, deleted, DAY, cents_per_hour=3200))
        await asyncio.sleep(0.5)
        assert not pending.done(), "the PUT waits for the delete"

        await deleting.commit()
        with pytest.raises(PartyNotFoundError):
            await asyncio.wait_for(pending, timeout=10)
    finally:
        await deleting.close()
        await writing.close()
