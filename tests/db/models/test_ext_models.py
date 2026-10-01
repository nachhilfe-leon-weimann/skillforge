import pytest
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


@pytest.mark.db
async def test_a_discord_account_names_its_party_by_id(session):
    from app.core.db.models import DiscordAccount, Party, PartyType

    party = Party(type=PartyType.PERSON)
    session.add(party)
    await session.flush()
    discord_account = DiscordAccount(discord_id=123456789012345678, party_id=party.id)
    session.add(discord_account)
    await session.flush()

    assert await session.get(DiscordAccount, 123456789012345678) is discord_account
    assert discord_account.party_id == party.id


def test_party_and_discord_account_know_nothing_of_each_other():
    """A Discord link is identity, written only through auth (ADR 0009); like a user account, it is no part of the
    party's ORM graph (ADR 0008)."""
    from app.core.db.models import DiscordAccount, Party

    assert [rel.key for rel in inspect(Party).relationships if rel.mapper.class_ is DiscordAccount] == []
    assert list(inspect(DiscordAccount).relationships) == []


def test_discord_account_indexes():
    from app.core.db.models import DiscordAccount

    indexes = {index.name: index for index in DiscordAccount.__table__.indexes}  # type: ignore -> exists at runtime

    party_active_index = indexes["ix_discord_account_party_id_active"]
    assert tuple(party_active_index.columns.keys()) == ("party_id", "active")
    assert party_active_index.unique is False

    primary_active_index = indexes["uq_discord_account_party_id_primary_active"]
    assert tuple(primary_active_index.columns.keys()) == ("party_id",)
    assert primary_active_index.unique is True

    where = primary_active_index.dialect_options["postgresql"]["where"]
    compiled_where = str(
        where.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert compiled_where == "ext.discord_account.is_primary IS true AND ext.discord_account.active IS true"


@pytest.mark.db
async def test_tutor_ms_account_relationship(session):
    from app.core.db.models import MicrosoftAccount, Party, PartyType, Person, Tutor

    def build_tutor() -> Tutor:
        party = Party(type=PartyType.PERSON)
        person = Person(firstname="Max", lastname="Mustermann", party=party)
        session.add_all([party, person])
        return Tutor(person=person)

    tutor = build_tutor()
    ms_account = MicrosoftAccount(user_id="SOME_MS_GRAPH_USER_ID", party=tutor.person.party)

    session.add_all([tutor, ms_account])
    await session.flush()

    assert ms_account.party_id == tutor.person.party_id
    assert tutor.person.party.microsoft_account is ms_account


@pytest.mark.db
async def test_student_ms_contact_relationship(session):
    from app.core.db.models import MicrosoftContact, Party, PartyType, Person, PreferredMeetingTool, Student

    def build_student() -> Student:
        party = Party(type=PartyType.PERSON)
        person = Person(firstname="Max", lastname="Mustermann", party=party)
        session.add_all([party, person])
        return Student(preferred_meeting_tool=PreferredMeetingTool.MICROSOFT_TEAMS, person=person)

    student = build_student()
    ms_contact = MicrosoftContact(contact_id="SOME_MS_GRAPH_CONTACT_ID", party=student.person.party)

    session.add_all([student, ms_contact])
    await session.flush()

    assert ms_contact.party_id == student.person.party_id
    assert student.person.party.microsoft_contact is ms_contact
