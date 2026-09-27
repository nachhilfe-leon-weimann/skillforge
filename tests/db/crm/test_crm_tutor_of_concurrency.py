"""A ``tutor_of`` PUT or DELETE racing the removal of one of its roles (decision O of bot-decoupling).

Every ``tutor_of`` must stand on both roles, also when the two requests overlap, and no two of these writes may
deadlock. Committed data, as in ``test_crm_roles_concurrency.py``: every test deletes its parties, and their roles and
relations cascade.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Database
from app.core.db.models import PartyRelation, PartyRelationType, PreferredMeetingTool
from app.services.crm import parties, persons, relations, roles
from app.services.crm.errors import InvalidPartyRelationError, PartyRelationNotFoundError
from app.services.crm.inputs import RelationDirection, StudentRoleData, TutorRoleData
from tests.db.auth.overlap import overlapping

pytestmark = pytest.mark.db

type Call = Callable[[AsyncSession], Coroutine[object, object, object]]


@pytest.fixture
async def pair(db: Database) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """A committed tutor and student, not yet related; both parties are deleted afterwards."""
    async with db.session() as setup:
        tutor = await persons.create_person(setup, firstname="Tom", lastname="Race", tutor=TutorRoleData())
        student = await persons.create_person(
            setup, firstname="Mia", lastname="Race", student=StudentRoleData(PreferredMeetingTool.DISCORD)
        )
    try:
        yield tutor.id, student.id
    finally:
        async with db.session() as cleanup:
            for party_id in (tutor.id, student.id):
                await parties.delete_party(cleanup, party_id)


@pytest.fixture
async def both_roles(db: Database) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """Two committed persons holding both roles, in ID order; both parties are deleted afterwards."""
    async with db.session() as setup:
        created = [
            await persons.create_person(
                setup,
                firstname=firstname,
                lastname="Race",
                student=StudentRoleData(PreferredMeetingTool.DISCORD),
                tutor=TutorRoleData(),
            )
            for firstname in ("Ada", "Bo")
        ]
    low, high = sorted(person.id for person in created)
    try:
        yield low, high
    finally:
        async with db.session() as cleanup:
            for party_id in (low, high):
                await parties.delete_party(cleanup, party_id)


def _relate(tutor_id: uuid.UUID, student_id: uuid.UUID) -> Call:
    async def relate(session: AsyncSession) -> object:
        return await relations.put_relation(session, tutor_id, PartyRelationType.TUTOR_OF, student_id)

    return relate


def _unrelate(tutor_id: uuid.UUID, student_id: uuid.UUID) -> Call:
    async def unrelate(session: AsyncSession) -> object:
        return await relations.remove_relation(session, tutor_id, PartyRelationType.TUTOR_OF, student_id)

    return unrelate


def _remove(role: str, party_id: uuid.UUID) -> Call:
    async def remove(session: AsyncSession) -> object:
        if role == "tutor":
            return await roles.remove_tutor_role(session, party_id)
        return await roles.remove_student_role(session, party_id)

    return remove


async def _tutor_of_count(db: Database) -> int:
    async with db.session(write=False) as check:
        count = await check.scalar(
            select(func.count()).select_from(PartyRelation).where(PartyRelation.type == PartyRelationType.TUTOR_OF)
        )
    return count or 0


@pytest.mark.parametrize("role", ["tutor", "student"])
async def test_a_tutor_of_put_waiting_on_a_role_removal_finds_the_role_gone(
    db: Database, pair: tuple[uuid.UUID, uuid.UUID], role: str
):
    tutor_id, student_id = pair

    put = await overlapping(db, _remove(role, tutor_id if role == "tutor" else student_id), _relate(*pair))

    assert isinstance(put.exception(), InvalidPartyRelationError)
    assert await _tutor_of_count(db) == 0


@pytest.mark.parametrize("role", ["tutor", "student"])
async def test_a_role_removal_waiting_on_a_tutor_of_put_takes_the_new_relation_along(
    db: Database, pair: tuple[uuid.UUID, uuid.UUID], role: str
):
    tutor_id, student_id = pair

    removal = await overlapping(db, _relate(*pair), _remove(role, tutor_id if role == "tutor" else student_id))

    assert removal.exception() is None
    assert await _tutor_of_count(db) == 0


@pytest.mark.parametrize("role", ["tutor", "student"])
@pytest.mark.parametrize("racer", ["put", "delete"])
async def test_a_write_of_the_pair_racing_the_removal_of_its_role_waits_instead_of_deadlocking(
    db: Database, both_roles: tuple[uuid.UUID, uuid.UUID], role: str, racer: str, monkeypatch: pytest.MonkeyPatch
):
    """The removal locks the person and the other side of the ``tutor_of`` it takes along, in ID order - the order
    in which a repeated PUT or a DELETE of that pair locks it. Were the removal to lock the person first and the other
    side only when ``saved`` moves it, a racer that locked the other side first (it sorts first here) would wait for
    the person while the removal waits for the other side: a deadlock, and Postgres fails one of the two."""
    low, high = both_roles
    person, other = high, low
    tutor_id, student_id = (person, other) if role == "tutor" else (other, person)
    async with db.session() as setup:
        await relations.put_relation(setup, tutor_id, PartyRelationType.TUTOR_OF, student_id)

    paused, go = asyncio.Event(), asyncio.Event()
    remove_tutor_of = roles._remove_tutor_of

    async def remove_tutor_of_once_the_racer_waits(
        session: AsyncSession, party_id: uuid.UUID, direction: RelationDirection
    ) -> list[uuid.UUID]:
        paused.set()
        await go.wait()
        return await remove_tutor_of(session, party_id, direction)

    monkeypatch.setattr(roles, "_remove_tutor_of", remove_tutor_of_once_the_racer_waits)
    remover: AsyncSession = db.session_factory()
    racer_session: AsyncSession = db.session_factory()
    try:
        removal = asyncio.create_task(_remove(role, person)(remover))
        await asyncio.wait_for(paused.wait(), timeout=5)
        write = _relate(tutor_id, student_id) if racer == "put" else _unrelate(tutor_id, student_id)
        raced = asyncio.create_task(write(racer_session))
        await asyncio.sleep(0.5)
        assert not raced.done(), "the racer waits for the removal"

        go.set()
        await asyncio.wait_for(removal, timeout=10)
        await remover.commit()
        await asyncio.wait([raced], timeout=10)
        await racer_session.rollback()
    finally:
        await remover.close()
        await racer_session.close()

    expected = InvalidPartyRelationError if racer == "put" else PartyRelationNotFoundError
    assert isinstance(raced.exception(), expected)
    assert await _tutor_of_count(db) == 0
