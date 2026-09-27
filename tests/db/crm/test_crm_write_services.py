"""The two rules every CRM write service follows (decisions H and M of the CRM API spec).

- **Reload rule:** the result can be mapped with ``party_detail()`` without a lazy load, which async
  SQLAlchemy would answer with ``MissingGreenlet``.
- **Aggregate root:** the write moves ``party.updated_at`` of exactly the parties it touches - "What moves what"
  in the bot-decoupling spec; a related bystander keeps its stamp.

``WRITES`` holds one scenario per write service function; ``test_every_write_service_has_a_scenario``
fails when a slice adds a function without adding it here.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from types import FunctionType
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.crm.schemas import ContactInfoResponse, PersonDetail, RelationResponse, party_detail
from app.core.db.models import (
    ContactInfo,
    ContactInfoType,
    Party,
    PartyRelation,
    PartyRelationType,
    PreferredMeetingTool,
    Student,
    StudentSubject,
    Tutor,
    TutorSubject,
)
from app.services import crm as crm_services
from app.services.crm import companies, contact_infos, parties, persons, relations, roles, subjects
from app.services.crm.inputs import NewContactInfo, StudentRoleData, TutorRoleData

pytestmark = pytest.mark.db

# Public service functions that are not writes inside the party aggregate. Every other public
# coroutine of ``app.services.crm`` - in whatever module a slice adds - needs a scenario in WRITES.
NOT_AGGREGATE_WRITES = {
    parties.load_party,  # the loading path itself
    parties.list_parties,
    relations.list_relations,
    parties.saved,  # the bookkeeping every write ends with
    persons.get_person,
    companies.get_company,
    # Subjects are reference data outside the aggregate: no party, no updated_at.
    subjects.list_subjects,
    subjects.get_subject,
    subjects.create_subject,
    subjects.update_subject,
    subjects.delete_subject,
    subjects.require_subjects,
}


@dataclass(frozen=True)
class Seed:
    """What exists before a write runs; every party's ``updated_at`` lies in the past."""

    person_id: uuid.UUID
    company_id: uuid.UUID
    other_id: uuid.UUID
    """A second person, on the other side of the ``tutor_of`` a role removal takes along."""
    bystander_id: uuid.UUID
    """The person's parent: related, so a write that moved every related party would move it too."""

    @property
    def parties(self) -> frozenset[uuid.UUID]:
        return frozenset({self.person_id, self.company_id, self.other_id, self.bystander_id})


@dataclass(frozen=True)
class Write:
    service: FunctionType
    """The service function the scenario exercises; the completeness check compares these."""
    run: Callable[[AsyncSession, Seed], Awaitable[Any]]
    touches: Callable[[Seed], tuple[uuid.UUID, ...]] = lambda seed: ()
    """The existing parties whose aggregate the write changes."""
    creates: bool = False
    """The write creates its party, so there is no earlier ``updated_at`` to move."""
    label: str = ""

    @property
    def id(self) -> str:
        return f"{self.service.__module__.rsplit('.', 1)[-1]}.{self.service.__name__}{self.label}"


CONTACT_INFOS = [
    NewContactInfo(ContactInfoType.EMAIL, "Max.Mustermann@Example.com", "private"),
    NewContactInfo(ContactInfoType.PHONE, "0151 234 567"),
]


async def _delete_the_company_that_pays_for_the_person(session: AsyncSession, seed: Seed) -> None:
    session.add(
        PartyRelation(from_party_id=seed.company_id, to_party_id=seed.person_id, type=PartyRelationType.PAYS_FOR)
    )
    await session.flush()
    await parties.delete_party(session, seed.company_id)


async def _subject_ids(session: AsyncSession, *titles: str) -> frozenset[int]:
    return frozenset([(await subjects.create_subject(session, title=title)).id for title in titles])


async def _put_student_role(session: AsyncSession, seed: Seed) -> Party:
    return await roles.put_student_role(
        session,
        seed.person_id,
        preferred_meeting_tool=PreferredMeetingTool.PHONE,
        subject_ids=await _subject_ids(session, "Mathematics", "Art"),
    )


async def _replace_student_role(session: AsyncSession, seed: Seed) -> Party:
    # Built through the ORM: a role created through the service would already have moved updated_at,
    # and the replacing PUT could no longer be told apart.
    old = await _subject_ids(session, "Mathematics", "Art")
    session.add(
        Student(
            person_id=seed.person_id,
            preferred_meeting_tool=PreferredMeetingTool.DISCORD,
            student_subjects=[StudentSubject(subject_id=subject_id) for subject_id in sorted(old)],
        )
    )
    await session.flush()
    session.expunge_all()
    return await roles.put_student_role(
        session,
        seed.person_id,
        preferred_meeting_tool=PreferredMeetingTool.PHONE,
        subject_ids=await _subject_ids(session, "Physics"),
    )


async def _change_only_the_meeting_tool(session: AsyncSession, seed: Seed) -> Party:
    session.add(Student(person_id=seed.person_id, preferred_meeting_tool=PreferredMeetingTool.DISCORD))
    await session.flush()
    session.expunge_all()
    return await roles.put_student_role(session, seed.person_id, preferred_meeting_tool=PreferredMeetingTool.PHONE)


async def _replace_tutor_role(session: AsyncSession, seed: Seed) -> Party:
    old = await _subject_ids(session, "Chemistry")
    session.add(Tutor(person_id=seed.person_id, tutor_subjects=[TutorSubject(subject_id=i) for i in old]))
    await session.flush()
    session.expunge_all()
    return await roles.put_tutor_role(session, seed.person_id, subject_ids=await _subject_ids(session, "Latin"))


async def _put_tutor_role(session: AsyncSession, seed: Seed) -> Party:
    return await roles.put_tutor_role(session, seed.person_id, subject_ids=await _subject_ids(session, "Chemistry"))


async def _remove_student_role(session: AsyncSession, seed: Seed) -> Party:
    session.add(Student(person_id=seed.person_id, preferred_meeting_tool=PreferredMeetingTool.DISCORD))
    await session.flush()
    session.expunge_all()
    return await roles.remove_student_role(session, seed.person_id)


async def _remove_tutor_role(session: AsyncSession, seed: Seed) -> Party:
    session.add(Tutor(person_id=seed.person_id))
    await session.flush()
    session.expunge_all()
    return await roles.remove_tutor_role(session, seed.person_id)


async def _remove_student_role_with_its_tutor_of(session: AsyncSession, seed: Seed) -> Party:
    session.add_all([
        Student(person_id=seed.person_id, preferred_meeting_tool=PreferredMeetingTool.DISCORD),
        Tutor(person_id=seed.other_id),
        PartyRelation(from_party_id=seed.other_id, to_party_id=seed.person_id, type=PartyRelationType.TUTOR_OF),
    ])
    await session.flush()
    session.expunge_all()
    return await roles.remove_student_role(session, seed.person_id)


async def _remove_tutor_role_with_its_tutor_of(session: AsyncSession, seed: Seed) -> Party:
    session.add_all([
        Tutor(person_id=seed.person_id),
        Student(person_id=seed.other_id, preferred_meeting_tool=PreferredMeetingTool.DISCORD),
        PartyRelation(from_party_id=seed.person_id, to_party_id=seed.other_id, type=PartyRelationType.TUTOR_OF),
    ])
    await session.flush()
    session.expunge_all()
    return await roles.remove_tutor_role(session, seed.person_id)


async def _first_contact_info_id(session: AsyncSession, party_id: uuid.UUID) -> uuid.UUID:
    contact_info_id = await session.scalar(
        select(ContactInfo.id).where(ContactInfo.party_id == party_id, ContactInfo.type == ContactInfoType.EMAIL)
    )
    assert contact_info_id is not None
    return contact_info_id


async def _add_contact_info(session: AsyncSession, seed: Seed) -> ContactInfoResponse:
    contact_info = await contact_infos.add_contact_info(
        session, seed.company_id, type=ContactInfoType.EMAIL, value="Office@Musterfirma.example", label="office"
    )
    return ContactInfoResponse.from_model(contact_info)


async def _update_contact_info(session: AsyncSession, seed: Seed) -> ContactInfoResponse:
    contact_info_id = await _first_contact_info_id(session, seed.person_id)
    contact_info = await contact_infos.update_contact_info(
        session, seed.person_id, contact_info_id, value="New@Example.com", label=None
    )
    return ContactInfoResponse.from_model(contact_info)


async def _remove_contact_info(session: AsyncSession, seed: Seed) -> None:
    contact_info_id = await _first_contact_info_id(session, seed.person_id)
    await contact_infos.remove_contact_info(session, seed.person_id, contact_info_id)


async def _put_relation(session: AsyncSession, seed: Seed) -> RelationResponse:
    view = await relations.put_relation(session, seed.company_id, PartyRelationType.PAYS_FOR, seed.person_id)
    return RelationResponse.from_view(view)


async def _remove_relation(session: AsyncSession, seed: Seed) -> None:
    session.add(
        PartyRelation(from_party_id=seed.company_id, to_party_id=seed.person_id, type=PartyRelationType.PAYS_FOR)
    )
    await session.flush()
    session.expunge_all()
    await relations.remove_relation(session, seed.company_id, PartyRelationType.PAYS_FOR, seed.person_id)


async def _create_person_with_roles(session: AsyncSession, seed: Seed) -> Party:
    return await persons.create_person(
        session,
        firstname="Erika",
        lastname="Musterfrau",
        contact_infos=CONTACT_INFOS,
        student=StudentRoleData(PreferredMeetingTool.DISCORD, await _subject_ids(session, "Biology", "art")),
        tutor=TutorRoleData(await _subject_ids(session, "Latin")),
    )


WRITES = [
    Write(
        persons.create_person,
        lambda session, seed: persons.create_person(session, firstname="Erika", lastname="Musterfrau"),
        creates=True,
        label="[without roles or contact infos]",
    ),
    Write(
        persons.create_person,
        lambda session, seed: persons.create_person(
            session, firstname="Erika", lastname="Musterfrau", contact_infos=CONTACT_INFOS
        ),
        creates=True,
        label="[with contact infos]",
    ),
    Write(persons.create_person, _create_person_with_roles, creates=True, label="[with both roles]"),
    Write(roles.put_student_role, _put_student_role, touches=lambda seed: (seed.person_id,)),
    Write(roles.put_student_role, _replace_student_role, touches=lambda seed: (seed.person_id,), label="[replace]"),
    Write(
        roles.put_student_role,
        _change_only_the_meeting_tool,
        touches=lambda seed: (seed.person_id,),
        label="[tool only]",
    ),
    Write(roles.put_tutor_role, _put_tutor_role, touches=lambda seed: (seed.person_id,)),
    Write(roles.put_tutor_role, _replace_tutor_role, touches=lambda seed: (seed.person_id,), label="[replace]"),
    Write(roles.remove_student_role, _remove_student_role, touches=lambda seed: (seed.person_id,)),
    Write(roles.remove_tutor_role, _remove_tutor_role, touches=lambda seed: (seed.person_id,)),
    # Decision O of bot-decoupling: the tutor_of goes with the role, and its other side moves too.
    Write(
        roles.remove_student_role,
        _remove_student_role_with_its_tutor_of,
        touches=lambda seed: (seed.person_id, seed.other_id),
        label="[with a tutor_of]",
    ),
    Write(
        roles.remove_tutor_role,
        _remove_tutor_role_with_its_tutor_of,
        touches=lambda seed: (seed.person_id, seed.other_id),
        label="[with a tutor_of]",
    ),
    # A relation belongs to both aggregates.
    Write(relations.put_relation, _put_relation, touches=lambda seed: (seed.company_id, seed.person_id)),
    Write(relations.remove_relation, _remove_relation, touches=lambda seed: (seed.company_id, seed.person_id)),
    # A child write returns the child: the scenario maps it, which must not lazy-load either.
    Write(contact_infos.add_contact_info, _add_contact_info, touches=lambda seed: (seed.company_id,)),
    Write(contact_infos.update_contact_info, _update_contact_info, touches=lambda seed: (seed.person_id,)),
    Write(contact_infos.remove_contact_info, _remove_contact_info, touches=lambda seed: (seed.person_id,)),
    Write(
        persons.update_person,
        lambda session, seed: persons.update_person(session, seed.person_id, firstname="Maximilian"),
        touches=lambda seed: (seed.person_id,),
    ),
    Write(
        parties.delete_party,
        _delete_the_company_that_pays_for_the_person,
        touches=lambda seed: (seed.person_id,),
    ),
    Write(
        companies.create_company,
        lambda session, seed: companies.create_company(session, name="Musterfirma GmbH", contact_infos=CONTACT_INFOS),
        creates=True,
    ),
    Write(
        companies.update_company,
        lambda session, seed: companies.update_company(session, seed.company_id, name="Musterfirma AG"),
        touches=lambda seed: (seed.company_id,),
    ),
]


@pytest.fixture
async def seed(session: AsyncSession, backdate) -> Seed:
    person = await persons.create_person(session, firstname="Max", lastname="Mustermann", contact_infos=CONTACT_INFOS)
    company = await companies.create_company(session, name="Musterfirma GmbH")
    other = await persons.create_person(session, firstname="Erika", lastname="Musterfrau")
    bystander = await persons.create_person(session, firstname="Berta", lastname="Mustermann")
    session.add(PartyRelation(from_party_id=bystander.id, to_party_id=person.id, type=PartyRelationType.PARENT_OF))
    seed = Seed(person_id=person.id, company_id=company.id, other_id=other.id, bystander_id=bystander.id)
    await backdate(*seed.parties)
    return seed


async def _stamps(session: AsyncSession, party_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, datetime]:
    """``updated_at`` of those of the parties that still exist, straight from the database."""
    rows = await session.execute(select(Party.id, Party.updated_at).where(Party.id.in_(party_ids)))
    return dict(rows.tuples().all())


@pytest.mark.parametrize("write", WRITES, ids=lambda write: write.id)
async def test_write_result_maps_to_the_detail_without_a_lazy_load(write: Write, session: AsyncSession, seed: Seed):
    result = await write.run(session, seed)

    if isinstance(result, Party):
        detail = party_detail(result)
        assert detail.id == result.id
        assert detail.updated_at == result.updated_at
    for party_id in write.touches(seed):
        assert party_detail(await parties.load_party(session, party_id)).id == party_id


@pytest.mark.parametrize("write", [write for write in WRITES if not write.creates], ids=lambda write: write.id)
async def test_write_moves_updated_at_of_every_party_it_touches(
    write: Write, session: AsyncSession, seed: Seed, updated_at
):
    touched = write.touches(seed)
    assert touched, "a write on an existing aggregate names the parties it touches"
    before = {party_id: await updated_at(party_id) for party_id in touched}

    result = await write.run(session, seed)

    for party_id in touched:
        assert await updated_at(party_id) > before[party_id]
    if isinstance(result, Party):
        assert party_detail(result).updated_at == await updated_at(result.id)


@pytest.mark.parametrize("create", ["person", "company"])
async def test_create_stores_normalized_contact_values_whatever_the_caller_passes(session: AsyncSession, create: str):
    """In-process callers bypass the request models, so the services normalize on their own."""
    if create == "person":
        party = await persons.create_person(session, firstname="Max", lastname="M", contact_infos=CONTACT_INFOS)
    else:
        party = await companies.create_company(session, name="Musterfirma GmbH", contact_infos=CONTACT_INFOS)

    stored = await session.execute(
        select(ContactInfo.type, ContactInfo.value, ContactInfo.label).where(ContactInfo.party_id == party.id)
    )
    assert sorted((type.value, value, label) for type, value, label in stored) == [
        ("email", "max.mustermann@example.com", "private"),
        ("phone", "+49151234567", None),
    ]
    assert [info.value for info in party_detail(party).contact_infos] == ["max.mustermann@example.com", "+49151234567"]


async def test_the_detail_maps_both_roles_with_their_subjects_in_title_order(session: AsyncSession, seed: Seed):
    """The roles get their routes with P0-4, but PARTY_GRAPH and the role mappers ship with the detail."""
    titles = ["physics", "Biology", "art"]
    created = {title: await subjects.create_subject(session, title=title) for title in titles}
    session.add(
        Student(
            person_id=seed.person_id,
            preferred_meeting_tool=PreferredMeetingTool.MICROSOFT_TEAMS,
            student_subjects=[StudentSubject(subject_id=created[title].id) for title in titles],
        )
    )
    session.add(
        Tutor(person_id=seed.person_id, tutor_subjects=[TutorSubject(subject_id=created["physics"].id)]),
    )
    await session.flush()
    session.expunge_all()

    detail = party_detail(await parties.load_party(session, seed.person_id))

    assert isinstance(detail, PersonDetail)
    assert detail.student is not None and detail.tutor is not None
    assert detail.student.preferred_meeting_tool is PreferredMeetingTool.MICROSOFT_TEAMS
    assert [subject.title for subject in detail.student.subjects] == ["art", "Biology", "physics"]
    assert [(subject.id, subject.title) for subject in detail.tutor.subjects] == [(created["physics"].id, "physics")]
    # A write on a person holding roles reloads them as well.
    updated = party_detail(await persons.update_person(session, seed.person_id, firstname="Maximilian"))
    assert isinstance(updated, PersonDetail)
    assert updated.student == detail.student and updated.tutor == detail.tutor


@pytest.mark.parametrize("write", WRITES, ids=lambda write: write.id)
async def test_write_moves_no_party_beyond_those_it_touches(write: Write, session: AsyncSession, seed: Seed):
    untouched = seed.parties - set(write.touches(seed))
    before = await _stamps(session, untouched)

    await write.run(session, seed)

    after = await _stamps(session, untouched)
    assert seed.bystander_id in after
    assert after == {party_id: before[party_id] for party_id in after}


async def test_subject_writes_move_no_party(session: AsyncSession, seed: Seed):
    """Subjects are reference data: renaming one that both roles hold moves neither person."""
    held = await subjects.create_subject(session, title="Mathematics")
    session.add_all([
        Student(
            person_id=seed.person_id,
            preferred_meeting_tool=PreferredMeetingTool.DISCORD,
            student_subjects=[StudentSubject(subject_id=held.id)],
        ),
        Tutor(person_id=seed.other_id, tutor_subjects=[TutorSubject(subject_id=held.id)]),
    ])
    await session.flush()
    before = await _stamps(session, seed.parties)

    await subjects.update_subject(session, held.id, title="Maths")
    spare = await subjects.create_subject(session, title="Latin")
    await subjects.delete_subject(session, spare.id)

    assert await _stamps(session, seed.parties) == before


@pytest.mark.parametrize("target", ["person", "company"])
async def test_an_update_with_nothing_to_change_is_not_a_write(
    target: str, session: AsyncSession, seed: Seed, updated_at
):
    party_id = seed.person_id if target == "person" else seed.company_id
    before = await updated_at(party_id)

    if target == "person":
        party = await persons.update_person(session, party_id)
    else:
        party = await companies.update_company(session, party_id)

    assert await updated_at(party_id) == before
    assert party_detail(party).updated_at == before


def _public_service_coroutines() -> set[FunctionType]:
    modules = [
        importlib.import_module(module.name)
        for module in pkgutil.walk_packages(crm_services.__path__, prefix=f"{crm_services.__name__}.")
    ]
    return {
        function
        for module in modules
        for name, function in vars(module).items()
        if inspect.iscoroutinefunction(function) and not name.startswith("_") and function.__module__ == module.__name__
    }


def test_every_write_service_has_a_scenario():
    public = _public_service_coroutines()

    assert {persons.create_person, subjects.create_subject} <= public, "the discovery found the service modules"
    assert NOT_AGGREGATE_WRITES <= public, "the exclusion list names only functions that exist"
    assert public - NOT_AGGREGATE_WRITES == {write.service for write in WRITES}
