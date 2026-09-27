"""`DELETE /parties/{party_id}` - guarded by external links - against the real database."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import (
    ClockodoCustomer,
    ClockodoProject,
    Company,
    ContactInfo,
    DiscordAccount,
    MicrosoftAccount,
    MicrosoftContact,
    Party,
    PartyRelation,
    PartyRelationType,
    Person,
    PreferredMeetingTool,
    SevdeskContact,
    Student,
    StudentSubject,
    Subject,
    Tutor,
    TutorSubject,
)

pytestmark = pytest.mark.db

LINKS = {
    "discord_account": lambda party_id: DiscordAccount(discord_id=4711, party_id=party_id),
    "sevdesk_contact": lambda party_id: SevdeskContact(sevdesk_id="sev-1", party_id=party_id),
    "clockodo_customer": lambda party_id: ClockodoCustomer(clockodo_customer_id="cc-1", party_id=party_id),
    "clockodo_project": lambda party_id: ClockodoProject(clockodo_project_id="cp-1", party_id=party_id),
    "microsoft_account": lambda party_id: MicrosoftAccount(user_id="ms-user-1", party_id=party_id),
    "microsoft_contact": lambda party_id: MicrosoftContact(contact_id="ms-contact-1", party_id=party_id),
}


async def _person(client: AsyncClient, firstname: str = "Max", **body) -> dict:
    response = await client.post("/persons", json={"firstname": firstname, "lastname": "Mustermann", **body})
    assert response.status_code == 201, response.text
    return response.json()


async def _count(session: AsyncSession, model, *where) -> int:
    return await session.scalar(select(func.count()).select_from(model).where(*where)) or 0


@pytest.mark.parametrize("kind", LINKS)
async def test_delete_is_409_party_in_use_while_a_link_exists_and_names_its_kind(
    client: AsyncClient, session: AsyncSession, kind: str
):
    person = await _person(client)
    party_id = uuid.UUID(person["id"])
    link = LINKS[kind](party_id)
    session.add(link)
    await session.flush()

    response = await client.delete(f"/parties/{party_id}")

    assert response.status_code == 409
    assert response.json() == {"detail": f"Party is linked to external systems: {kind}", "code": "party_in_use"}
    assert await _count(session, Party, Party.id == party_id) == 1
    assert await _count(session, type(link), type(link).party_id == party_id) == 1
    assert (await client.get(f"/parties/{party_id}")).json() == person


async def test_delete_names_every_link_kind_in_a_fixed_order_without_their_identifiers(
    client: AsyncClient, session: AsyncSession
):
    party_id = uuid.UUID((await _person(client))["id"])
    for kind in ("microsoft_contact", "sevdesk_contact", "discord_account"):
        session.add(LINKS[kind](party_id))
    await session.flush()

    response = await client.delete(f"/parties/{party_id}")

    assert response.json() == {
        "detail": "Party is linked to external systems: discord_account, sevdesk_contact, microsoft_contact",
        "code": "party_in_use",
    }
    assert "4711" not in response.text and "sev-1" not in response.text


async def test_a_deactivated_discord_link_goes_with_its_party(client: AsyncClient, session: AsyncSession):
    party_id = uuid.UUID((await _person(client))["id"])
    session.add(DiscordAccount(discord_id=4711, party_id=party_id, active=False, is_primary=False))
    await session.flush()

    assert (await client.delete(f"/parties/{party_id}")).status_code == 204
    remaining = await session.scalar(
        select(DiscordAccount).where(DiscordAccount.discord_id == 4711).execution_options(populate_existing=True)
    )
    assert remaining is None


async def test_a_link_of_another_party_does_not_guard_this_one(client: AsyncClient, session: AsyncSession):
    linked = uuid.UUID((await _person(client, "Erika"))["id"])
    free = uuid.UUID((await _person(client))["id"])
    session.add(LINKS["sevdesk_contact"](linked))
    await session.flush()

    assert (await client.delete(f"/parties/{free}")).status_code == 204
    assert (await client.delete(f"/parties/{linked}")).status_code == 409


async def test_delete_answers_204_and_takes_roles_contact_infos_and_relations_along(
    client: AsyncClient, session: AsyncSession, backdate, updated_at
):
    subject = Subject(title="Mathematics")
    session.add(subject)
    await session.flush()
    person = await _person(client, contact_infos=[{"type": "email", "value": "max@example.com"}])
    parent = await _person(client, "Erika")
    company = (await client.post("/companies", json={"name": "Musterfirma GmbH"})).json()
    party_id, parent_id, company_id = (uuid.UUID(party["id"]) for party in (person, parent, company))
    session.add_all([
        Student(
            person_id=party_id,
            preferred_meeting_tool=PreferredMeetingTool.DISCORD,
            student_subjects=[StudentSubject(subject_id=subject.id)],
        ),
        Tutor(person_id=party_id, tutor_subjects=[TutorSubject(subject_id=subject.id)]),
        PartyRelation(from_party_id=parent_id, to_party_id=party_id, type=PartyRelationType.PARENT_OF),
        PartyRelation(from_party_id=company_id, to_party_id=party_id, type=PartyRelationType.PAYS_FOR),
        PartyRelation(from_party_id=party_id, to_party_id=parent_id, type=PartyRelationType.TUTOR_OF),
    ])
    await backdate(party_id, parent_id, company_id)
    before = {related: await updated_at(related) for related in (parent_id, company_id)}

    response = await client.delete(f"/parties/{party_id}")

    assert response.status_code == 204
    assert response.content == b""
    assert (await client.get(f"/parties/{party_id}")).status_code == 404
    assert await _count(session, Party, Party.id == party_id) == 0
    assert await _count(session, Person, Person.party_id == party_id) == 0
    assert await _count(session, Student) == 0
    assert await _count(session, Tutor) == 0
    assert await _count(session, StudentSubject) == 0
    assert await _count(session, TutorSubject) == 0
    assert await _count(session, ContactInfo, ContactInfo.party_id == party_id) == 0
    touching = or_(PartyRelation.from_party_id == party_id, PartyRelation.to_party_id == party_id)
    assert await _count(session, PartyRelation, touching) == 0
    assert await _count(session, PartyRelation) == 0
    # What the party pointed at stays, and the parties on the other side of its relations changed.
    assert await _count(session, Subject) == 1
    assert await _count(session, Party) == 2
    assert all([await updated_at(related) > before[related] for related in (parent_id, company_id)])


async def test_delete_of_a_company_removes_its_company_row(client: AsyncClient, session: AsyncSession):
    company = (await client.post("/companies", json={"name": "Musterfirma GmbH"})).json()

    response = await client.delete(f"/parties/{company['id']}")

    assert response.status_code == 204
    assert await _count(session, Company) == 0
    assert await _count(session, Party) == 0


async def test_delete_of_an_unknown_party_is_404_and_so_is_deleting_twice(client: AsyncClient):
    person = await _person(client)
    expected = {"detail": "Party not found", "code": "party_not_found"}

    unknown = await client.delete(f"/parties/{uuid.uuid4()}")
    first = await client.delete(f"/parties/{person['id']}")
    second = await client.delete(f"/parties/{person['id']}")

    assert (unknown.status_code, unknown.json()) == (404, expected)
    assert first.status_code == 204
    assert (second.status_code, second.json()) == (404, expected)


async def test_the_guard_locks_the_party_row_before_it_looks_for_links(client: AsyncClient, statements: list[str]):
    """Every foreign key into core.party cascades: a link created between check and delete would vanish."""
    person = await _person(client)
    statements.clear()

    await client.delete(f"/parties/{person['id']}")

    lock = next(index for index, statement in enumerate(statements) if "FOR UPDATE" in statement)
    guard = next(index for index, statement in enumerate(statements) if "ext.discord_account" in statement)
    delete = next(index for index, statement in enumerate(statements) if statement.startswith("DELETE FROM core.party"))
    assert lock < guard < delete
    assert "FROM core.party" in statements[lock]
    assert not any(
        statement.startswith(("DELETE FROM ext.", "UPDATE ext.", "INSERT INTO ext.")) for statement in statements
    )
