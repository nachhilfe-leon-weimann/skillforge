"""The calendar import across real transactions: overlapping imports run one after the other - no occurrence and no
lesson twice, no 500.

These tests commit, so they cannot run on the rolled-back ``session`` fixture; they remove what they created.
"""

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, func, select

from app.core.db import Database
from app.core.db.models import CalendarEvent, Lesson, Party
from app.services.billing import calendar
from tests.db.billing.calendar_support import MAX, NOW, answer_view, fixture_events, graph_settings, new_student, reader
from tests.integrations.graph.fake_graph import Credential, FakeGraph

pytestmark = pytest.mark.db

SERIES = [event["id"] for event in fixture_events("g2-series-first-read")]


@pytest.fixture
async def student(db: Database) -> AsyncIterator[uuid.UUID]:
    """A committed synthetic student; afterwards the occurrences, its lessons and the party are removed."""
    async with db.session() as setup:
        student_id = await new_student(setup, MAX)
    try:
        yield student_id
    finally:
        async with db.session() as cleanup:
            await cleanup.execute(delete(Lesson).where(Lesson.student_party_id == student_id))
            await cleanup.execute(delete(CalendarEvent).where(CalendarEvent.external_id.in_(SERIES)))
            await cleanup.execute(delete(Party).where(Party.id == student_id))


async def test_overlapping_imports_wait_for_each_other_and_store_every_occurrence_once(
    db: Database, student: uuid.UUID
):
    fake = FakeGraph()
    answer_view(fake, fixture_events("g2-series-first-read"))
    read = reader(fake, graph_settings(Credential.generate()))
    first, second = db.session_factory(), db.session_factory()
    try:
        # The first import has written its occurrences and lessons but not committed yet ...
        await calendar.import_calendar(
            first, read, first_day=dt.date(2026, 11, 23), last_day=dt.date(2026, 12, 13), now=NOW
        )
        # ... when an import of an overlapping range arrives: it waits for the first one's lock.
        pending = asyncio.create_task(
            calendar.import_calendar(
                second, read, first_day=dt.date(2026, 11, 30), last_day=dt.date(2026, 12, 6), now=NOW
            )
        )
        await asyncio.sleep(0.5)
        assert not pending.done(), "the second import waits for the first"

        await first.commit()
        counts = await asyncio.wait_for(pending, timeout=10)
        await second.commit()
    finally:
        await first.close()
        await second.close()

    # The read returns the whole series; the second import finds every occurrence stored by the first.
    assert (counts.occurrences, counts.lessons_created) == (3, 0)
    async with db.session(write=False) as check:
        stored = await check.scalar(select(func.count()).where(CalendarEvent.external_id.in_(SERIES)))
        lessons = await check.scalar(select(func.count()).where(Lesson.student_party_id == student))
    assert (stored, lessons) == (3, 3)
