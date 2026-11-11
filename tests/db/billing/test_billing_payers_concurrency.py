"""Overlapping payer writes answer by a service rule, never by a database constraint as a 500.

Two links of one sevDesk contact answer `sevdesk_contact_already_linked` or the link; a lesson that stores its payer
while that payer is deleted stores the payer the CRM resolves after the delete.

This needs two real transactions, so it cannot run on the rolled-back ``session`` fixture: it commits, and removes
what it created.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Database
from app.core.db.models import Lesson, LessonStatus, PartyRelationType, PreferredMeetingTool, SevdeskContact
from app.services.billing import lessons, payers
from app.services.billing.errors import SevdeskContactAlreadyLinkedError
from app.services.crm import parties, persons, relations
from app.services.crm.errors import PartyInUseError, PartyNotFoundError
from app.services.crm.inputs import StudentRoleData

pytestmark = pytest.mark.db

CONTACT = "4711003"


async def _race(db: Database, first_party: uuid.UUID, second_party: uuid.UUID) -> SevdeskContact:
    """Link the contact to ``first_party`` without committing, start linking it to ``second_party``, see that wait,
    commit the first and return what the second answers."""
    first, second = db.session_factory(), db.session_factory()
    try:
        await payers.link_sevdesk_contact(first, first_party, CONTACT)
        racing = asyncio.create_task(payers.link_sevdesk_contact(second, second_party, CONTACT))
        await asyncio.sleep(0.5)
        assert not racing.done(), "the second link waits for the first"

        await first.commit()
        return await asyncio.wait_for(racing, timeout=10)
    finally:
        await first.close()
        await second.close()


@pytest.fixture
async def two_parties(db: Database):
    async with db.session() as setup:
        ids = [(await persons.create_person(setup, firstname=name, lastname="A")).id for name in ("Race", "Other")]
    yield ids
    async with db.session() as cleanup:
        await cleanup.execute(delete(SevdeskContact).where(SevdeskContact.sevdesk_id == CONTACT))
        for party_id in ids:
            await parties.delete_party(cleanup, party_id)


async def test_linking_one_contact_to_two_parties_at_once_is_already_linked(db: Database, two_parties):
    party, other = two_parties

    with pytest.raises(SevdeskContactAlreadyLinkedError):
        await _race(db, party, other)


async def test_the_same_link_twice_at_once_answers_the_link_both_times(db: Database, two_parties):
    party, _ = two_parties

    link = await _race(db, party, party)

    assert (link.sevdesk_id, link.party_id) == (CONTACT, party)


# --- A lesson's payer against a delete of that payer (PD1, PD9) ---

START = datetime(2026, 3, 29, 0, 30, tzinfo=UTC)


@dataclass(frozen=True)
class Family:
    student_id: uuid.UUID
    payer_id: uuid.UUID


@pytest.fixture
async def family(db: Database) -> AsyncIterator[Family]:
    """A committed student and a party with nothing but a `pays_for` to it; afterwards all of it is removed."""
    async with db.session() as setup:
        student = await persons.create_person(
            setup,
            firstname="Race",
            lastname="B",
            student=StudentRoleData(preferred_meeting_tool=PreferredMeetingTool.DISCORD),
        )
        payer = await persons.create_person(setup, firstname="Race", lastname="A")
    yield Family(student.id, payer.id)
    async with db.session() as cleanup:
        await cleanup.execute(delete(Lesson).where(Lesson.student_party_id == student.id))
        for party_id in (student.id, payer.id):
            try:
                await parties.delete_party(cleanup, party_id)
            except PartyNotFoundError:
                pass


async def _lesson(db: Database, family: Family, *, held: bool) -> uuid.UUID:
    """A committed lesson of the student, ticked or not; the `pays_for` is put afterwards, so a ticked lesson stores
    the student as its payer."""
    async with db.session() as setup:
        lesson = await lessons.create_lesson(
            setup, student_id=family.student_id, subject="Mathe", starts_at=START, minutes=60
        )
        if held:
            await lessons.update_lesson(setup, lesson.id, status=LessonStatus.HELD)
        await relations.put_relation(setup, family.payer_id, PartyRelationType.PAYS_FOR, family.student_id)
    return lesson.id


async def _write_while_the_payer_is_deleted(
    db: Database, family: Family, write: Callable[[AsyncSession], Coroutine[Any, Any, Lesson]]
) -> uuid.UUID | None:
    """Delete the payer without committing - its guard passed, no billing row names it yet - start ``write``, see it
    wait, commit the delete and return the payer the written lesson holds once it committed."""
    deleting, writing = db.session_factory(), db.session_factory()
    try:
        await parties.delete_party(deleting, family.payer_id)
        racing = asyncio.create_task(write(writing))
        await asyncio.sleep(0.5)
        assert not racing.done(), "the payer write waits for the delete"

        await deleting.commit()
        lesson = await asyncio.wait_for(racing, timeout=10)
        await writing.commit()
    finally:
        await deleting.close()
        await writing.close()
    async with db.session() as check:
        return await check.scalar(select(Lesson.payer_party_id).where(Lesson.id == lesson.id))


async def test_a_tick_while_its_payer_is_deleted_stores_the_payer_resolved_after_the_delete(
    db: Database, family: Family
):
    lesson_id = await _lesson(db, family, held=False)

    stored = await _write_while_the_payer_is_deleted(
        db, family, lambda session: lessons.update_lesson(session, lesson_id, status=LessonStatus.HELD)
    )

    # The pays_for went with the deleted payer: the student pays for itself.
    assert stored == family.student_id


async def test_resolve_payer_while_the_payer_is_deleted_stores_the_payer_resolved_after_the_delete(
    db: Database, family: Family
):
    lesson_id = await _lesson(db, family, held=True)

    stored = await _write_while_the_payer_is_deleted(
        db, family, lambda session: payers.resolve_lesson_payer(session, lesson_id)
    )

    assert stored == family.student_id


async def test_a_delete_of_the_payer_after_the_tick_waits_and_is_party_in_use(db: Database, family: Family):
    lesson_id = await _lesson(db, family, held=False)
    ticking, deleting = db.session_factory(), db.session_factory()
    try:
        # The tick holds its payer ...
        await lessons.update_lesson(ticking, lesson_id, status=LessonStatus.HELD)
        # ... so the delete waits for it, and then its guard sees the lesson naming the payer.
        racing = asyncio.create_task(parties.delete_party(deleting, family.payer_id))
        await asyncio.sleep(0.5)
        assert not racing.done(), "the delete waits for the tick"

        await ticking.commit()
        with pytest.raises(PartyInUseError, match="lesson_payer"):
            await asyncio.wait_for(racing, timeout=10)
    finally:
        await ticking.close()
        await deleting.close()
