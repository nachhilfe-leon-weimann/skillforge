"""Two overlapping adds of the same lesson - a client retrying - answer `lesson_already_exists`, not a 500.

This needs two real transactions, so it cannot run on the rolled-back ``session`` fixture: it commits, and removes
what it created.
"""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete

from app.core.db import Database
from app.core.db.models import Lesson, PreferredMeetingTool
from app.services.billing import lessons
from app.services.billing.errors import LessonAlreadyExistsError
from app.services.crm import parties, persons
from app.services.crm.inputs import StudentRoleData

pytestmark = pytest.mark.db

START = datetime(2026, 3, 29, 0, 30, tzinfo=UTC)


async def test_an_overlapping_add_of_the_same_lesson_waits_and_then_is_lesson_already_exists(db: Database):
    async with db.session() as setup:
        student = await persons.create_person(
            setup,
            firstname="Race",
            lastname="Condition",
            student=StudentRoleData(preferred_meeting_tool=PreferredMeetingTool.DISCORD),
        )

    async def add(session):
        return await lessons.create_lesson(session, student_id=student.id, subject="Mathe", starts_at=START, minutes=60)

    first, second = db.session_factory(), db.session_factory()
    try:
        # The first request has added the lesson but not committed yet ...
        await add(first)
        # ... when the retry arrives: no check-then-insert lets it pass, it waits on uq_lesson_student_start.
        retry = asyncio.create_task(add(second))
        await asyncio.sleep(0.5)
        assert not retry.done(), "the retry waits for the first request"

        await first.commit()
        with pytest.raises(LessonAlreadyExistsError):
            await asyncio.wait_for(retry, timeout=10)
    finally:
        await first.close()
        await second.close()
        async with db.session() as cleanup:
            await cleanup.execute(delete(Lesson).where(Lesson.student_party_id == student.id))
            await parties.delete_party(cleanup, student.id)
