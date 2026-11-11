"""`resolve_payer`: who pays for a student, read from the CRM's `pays_for` relations (billing spec, decision I)."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import PartyRelationType, PreferredMeetingTool
from app.services.crm import companies, persons, relations
from app.services.crm.errors import PartyNotFoundError
from app.services.crm.inputs import StudentRoleData
from app.services.crm.payers import PayerResolution, resolve_payer

pytestmark = pytest.mark.db


async def _student(session: AsyncSession) -> uuid.UUID:
    party = await persons.create_person(
        session,
        firstname="Student",
        lastname="B",
        student=StudentRoleData(preferred_meeting_tool=PreferredMeetingTool.DISCORD),
    )
    return party.id


async def _person(session: AsyncSession, firstname: str = "Parent") -> uuid.UUID:
    return (await persons.create_person(session, firstname=firstname, lastname="A")).id


async def _pays_for(session: AsyncSession, payer_id: uuid.UUID, student_id: uuid.UUID) -> None:
    await relations.put_relation(session, payer_id, PartyRelationType.PAYS_FOR, student_id)


async def test_without_pays_for_the_student_pays_for_itself(session: AsyncSession):
    student_id = await _student(session)
    # Relations of another type, and a pays_for the student holds for someone else, name no payer.
    parent_id = await _person(session)
    await relations.put_relation(session, parent_id, PartyRelationType.PARENT_OF, student_id)
    await _pays_for(session, student_id, await _student(session))

    resolution = await resolve_payer(session, student_id)

    assert resolution == PayerResolution(student_id=student_id, payer_id=student_id)
    assert not resolution.ambiguous


async def test_one_pays_for_names_that_person(session: AsyncSession):
    student_id, parent_id = await _student(session), await _person(session)
    await _pays_for(session, parent_id, student_id)

    assert await resolve_payer(session, student_id) == PayerResolution(student_id=student_id, payer_id=parent_id)


async def test_one_pays_for_names_that_company(session: AsyncSession):
    """An authority that pays is a company party with `pays_for` (decision I)."""
    student_id = await _student(session)
    authority = await companies.create_company(session, name="Authority C")
    await _pays_for(session, authority.id, student_id)

    assert await resolve_payer(session, student_id) == PayerResolution(student_id=student_id, payer_id=authority.id)


async def test_two_pays_for_are_ambiguous_and_name_no_payer(session: AsyncSession):
    student_id = await _student(session)
    authority = await companies.create_company(session, name="Authority C")
    await _pays_for(session, await _person(session), student_id)
    await _pays_for(session, authority.id, student_id)

    resolution = await resolve_payer(session, student_id)

    assert resolution == PayerResolution(student_id=student_id, payer_id=None)
    assert resolution.ambiguous


async def test_a_party_that_does_not_exist_is_party_not_found(session: AsyncSession):
    with pytest.raises(PartyNotFoundError):
        await resolve_payer(session, uuid.uuid4())
