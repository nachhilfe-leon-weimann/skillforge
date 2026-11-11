"""Overlapping writes answer by a service rule, never by a database constraint as a 500, and lose nothing.

This needs two real transactions, so it cannot run on the rolled-back ``session`` fixture: it commits, and removes
what it created.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from app.core.db import Database
from app.core.db.models import Lesson, LessonStatus, Party, PreferredMeetingTool
from app.services.billing import lessons
from app.services.billing.errors import LessonAlreadyExistsError, UnknownStudentError
from app.services.crm import parties, persons
from app.services.crm.inputs import StudentRoleData

pytestmark = pytest.mark.db

START = datetime(2026, 3, 29, 0, 30, tzinfo=UTC)


@pytest.fixture
async def students(db: Database) -> AsyncIterator[list[uuid.UUID]]:
    """Two committed synthetic students; afterwards their lessons and whatever is left of them are removed."""
    async with db.session() as setup:
        created = [
            await persons.create_person(
                setup,
                firstname="Race",
                lastname=lastname,
                student=StudentRoleData(preferred_meeting_tool=PreferredMeetingTool.DISCORD),
            )
            for lastname in ("A", "B")
        ]
    ids = [party.id for party in created]
    try:
        yield ids
    finally:
        async with db.session() as cleanup:
            await cleanup.execute(delete(Lesson).where(Lesson.student_party_id.in_(ids)))
            await cleanup.execute(delete(Party).where(Party.id.in_(ids)))


async def _add(session, student_id: uuid.UUID) -> Lesson:
    return await lessons.create_lesson(session, student_id=student_id, subject="Mathe", starts_at=START, minutes=60)


async def test_an_overlapping_add_of_the_same_lesson_waits_and_then_is_lesson_already_exists(
    db: Database, students: list[uuid.UUID]
):
    student = students[0]
    first, second = db.session_factory(), db.session_factory()
    try:
        # The first request has added the lesson but not committed yet ...
        await _add(first, student)
        # ... when the retry arrives: no check-then-insert lets it pass, it waits on uq_lesson_student_start.
        retry = asyncio.create_task(_add(second, student))
        await asyncio.sleep(0.5)
        assert not retry.done(), "the retry waits for the first request"

        await first.commit()
        with pytest.raises(LessonAlreadyExistsError):
            await asyncio.wait_for(retry, timeout=10)
    finally:
        await first.close()
        await second.close()


@pytest.mark.parametrize("write", ["add", "move"])
async def test_a_write_naming_a_student_whose_delete_is_under_way_waits_and_then_is_unknown_student(
    db: Database, students: list[uuid.UUID], write: str
):
    """The add (or the move to that student) waits for the delete and finds no party: `unknown_student`, not the
    foreign key's 500."""
    kept, deleted = students
    async with db.session() as setup:
        lesson_id = (await _add(setup, kept)).id

    async def name_the_deleted_student(session):
        if write == "add":
            return await _add(session, deleted)
        return await lessons.update_lesson(session, lesson_id, student_id=deleted)

    deleting, writing = db.session_factory(), db.session_factory()
    try:
        # The delete has removed the party but not committed yet ...
        await parties.delete_party(deleting, deleted)
        # ... when the write arrives: it waits on the party's lock instead of reading the party it still sees.
        pending = asyncio.create_task(name_the_deleted_student(writing))
        await asyncio.sleep(0.5)
        assert not pending.done(), "the write waits for the delete"

        await deleting.commit()
        with pytest.raises(UnknownStudentError):
            await asyncio.wait_for(pending, timeout=10)
    finally:
        await deleting.close()
        await writing.close()


async def test_an_overlapping_patch_waits_and_then_starts_from_the_others_row(db: Database, students: list[uuid.UUID]):
    """Without the lesson's row lock the second PATCH would compute its changes from the stale row and lose its
    explicit `billable: true`."""
    async with db.session() as setup:
        lesson_id = (await _add(setup, students[0])).id
        await lessons.update_lesson(setup, lesson_id, status=LessonStatus.HELD)

    first, second = db.session_factory(), db.session_factory()
    try:
        # Goodwill on the held lesson, not committed yet ...
        await lessons.update_lesson(first, lesson_id, billable=False)
        # ... when the maintainer bills it as a late cancellation.
        overlapping = asyncio.create_task(
            lessons.update_lesson(second, lesson_id, status=LessonStatus.CANCELLED, billable=True)
        )
        await asyncio.sleep(0.5)
        assert not overlapping.done(), "the second PATCH waits for the first"

        await first.commit()
        await asyncio.wait_for(overlapping, timeout=10)
        await second.commit()
    finally:
        await first.close()
        await second.close()

    async with db.session(write=False) as check:
        row = (await check.execute(select(Lesson.status, Lesson.billable).where(Lesson.id == lesson_id))).one()
    assert tuple(row) == (LessonStatus.CANCELLED, True)
