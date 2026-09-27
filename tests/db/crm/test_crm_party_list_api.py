"""`GET /parties` - list, search, filters, order and paging - against the real database."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import Party, PreferredMeetingTool, Student, StudentSubject, Subject, Tutor, TutorSubject

pytestmark = pytest.mark.db


async def _person(client: AsyncClient, firstname: str, lastname: str, *contact_infos: dict) -> dict:
    body = {"firstname": firstname, "lastname": lastname, "contact_infos": list(contact_infos)}
    response = await client.post("/persons", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _company(client: AsyncClient, name: str, *contact_infos: dict) -> dict:
    response = await client.post("/companies", json={"name": name, "contact_infos": list(contact_infos)})
    assert response.status_code == 201, response.text
    return response.json()


async def _subject(session: AsyncSession, title: str) -> int:
    subject = Subject(title=title)
    session.add(subject)
    await session.flush()
    return subject.id


async def _give_roles(
    session: AsyncSession, person: dict, *, student: list[int] | None = None, tutor: list[int] | None = None
) -> None:
    """The role routes arrive with P0-4; until then roles are built directly."""
    person_id = uuid.UUID(person["id"])
    if student is not None:
        session.add(
            Student(
                person_id=person_id,
                preferred_meeting_tool=PreferredMeetingTool.DISCORD,
                student_subjects=[StudentSubject(subject_id=subject_id) for subject_id in student],
            )
        )
    if tutor is not None:
        session.add(Tutor(person_id=person_id, tutor_subjects=[TutorSubject(subject_id=sid) for sid in tutor]))
    await session.flush()


async def _list(client: AsyncClient, **params) -> dict:
    response = await client.get("/parties", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _names(page: dict) -> list[str]:
    return [item["display_name"] for item in page["items"]]


def _email(value: str) -> dict:
    return {"type": "email", "value": value}


# --- search ---


async def test_q_requires_every_token_to_match(client: AsyncClient):
    await _person(client, "Max", "Mustermann")
    await _person(client, "Max", "Meier")
    await _person(client, "Erika", "Mustermann")

    assert _names(await _list(client, q="max muster")) == ["Max Mustermann"]
    assert _names(await _list(client, q="MUSTER max")) == ["Max Mustermann"]
    assert _names(await _list(client, q="max")) == ["Max Meier", "Max Mustermann"]
    assert (await _list(client, q="max muster"))["total"] == 1


async def test_q_matches_a_part_of_a_contact_value_and_finds_the_owner(client: AsyncClient):
    owner = await _person(client, "Max", "Mustermann", _email("max.mustermann@example.com"))
    await _person(client, "Erika", "Musterfrau", _email("erika@elsewhere.example"))
    company = await _company(client, "Musterfirma GmbH", {"type": "phone", "value": "+49 30 1234567"})

    assert [item["id"] for item in (await _list(client, q="mustermann@EXAMPLE"))["items"]] == [owner["id"]]
    assert [item["id"] for item in (await _list(client, q="301234"))["items"]] == [company["id"]]


async def test_q_finds_a_phone_number_by_its_stored_e164_digits(client: AsyncClient):
    """What the description of ``q`` promises: the national leading zero is not part of the stored form."""
    await _person(client, "Paula", "Phone-Search", {"type": "phone", "value": "0171 1234567"})

    assert _names(await _list(client, q="phone-search 171 1234567")) == ["Paula Phone-Search"]
    assert _names(await _list(client, q="+491711234567")) == ["Paula Phone-Search"]
    assert _names(await _list(client, q="phone-search 0171")) == []


async def test_q_matches_a_token_in_the_name_and_another_in_a_contact_value(client: AsyncClient):
    owner = await _person(client, "Max", "Mustermann", _email("mm@example.com"))
    await _person(client, "Max", "Meier", _email("meier@other.example"))

    assert [item["id"] for item in (await _list(client, q="max mm@example"))["items"]] == [owner["id"]]


async def test_q_matches_a_company_name(client: AsyncClient):
    await _company(client, "Musterfirma GmbH")
    await _person(client, "Max", "Mustermann")

    assert _names(await _list(client, q="firma")) == ["Musterfirma GmbH"]
    assert _names(await _list(client, q="muster")) == ["Musterfirma GmbH", "Max Mustermann"]


async def test_q_takes_like_wildcards_literally(client: AsyncClient):
    await _company(client, "100% Nachhilfe")
    await _company(client, "Nachhilfe_Plus")
    await _company(client, "Nachhilfe Nord")

    assert _names(await _list(client, q="0%")) == ["100% Nachhilfe"]
    assert _names(await _list(client, q="e_p")) == ["Nachhilfe_Plus"]
    assert _names(await _list(client, q="%%")) == []


async def test_a_blank_q_filters_nothing_and_a_single_character_is_rejected(client: AsyncClient):
    await _person(client, "Max", "Mustermann")

    assert (await _list(client, q="   "))["total"] == 1
    too_short = await client.get("/parties", params={"q": "m"})
    assert too_short.status_code == 422
    assert [error["loc"] for error in too_short.json()["errors"]] == [["query", "q"]]


async def test_an_oversized_q_is_a_validation_error_not_a_500(client: AsyncClient):
    """Every word becomes four bind parameters; a statement takes 32767 of them."""
    await _person(client, "Max", "Mustermann")

    too_long = await client.get("/parties", params={"q": "a " * 101})
    many_words = await client.get("/parties", params={"q": " ".join(["ma"] * 66)})

    assert too_long.status_code == 422
    assert [error["loc"] for error in too_long.json()["errors"]] == [["query", "q"]]
    assert (many_words.status_code, many_words.json()["total"]) == (200, 1)


async def test_q_with_a_nul_character_is_a_validation_error_not_a_500(client: AsyncClient):
    response = await client.get("/parties?q=ma%00x")

    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["errors"]] == [["query", "q"]]


# --- filters ---


async def test_filters_never_fail_an_impossible_combination_is_an_empty_page(
    client: AsyncClient, session: AsyncSession
):
    maths = await _subject(session, "Mathematics")
    await _give_roles(session, await _person(client, "Max", "Mustermann"), student=[maths])
    await _company(client, "Musterfirma GmbH")

    for params in (
        {"type": "company", "role": "student"},
        {"type": "company", "subject_id": maths},
        {"subject_id": maths + 1000},
        {"role": "tutor"},
        {"q": "nobody"},
    ):
        assert await _list(client, **params) == {"items": [], "total": 0, "limit": 50, "offset": 0}, params


@pytest.mark.parametrize(
    ("params", "loc"),
    [
        ({"rolle": "student"}, ["query", "rolle"]),
        ({"role": "parent"}, ["query", "role"]),
        ({"type": "family"}, ["query", "type"]),
        ({"subject_id": "maths"}, ["query", "subject_id"]),
        ({"subject_id": 2**31}, ["query", "subject_id"]),
        ({"limit": 0}, ["query", "limit"]),
        ({"offset": 2**63}, ["query", "offset"]),
        ({"updated_since": "2026-01-01T00:00:00"}, ["query", "updated_since"]),
        ({"updated_since": "yesterday"}, ["query", "updated_since"]),
    ],
    ids=[
        "unknown parameter",
        "unknown role",
        "unknown type",
        "non-numeric subject",
        "subject out of range",
        "limit",
        "offset beyond int64",
        "updated_since without an offset",
        "updated_since that is no timestamp",
    ],
)
async def test_a_malformed_query_is_the_validation_422(client: AsyncClient, params: dict, loc: list):
    response = await client.get("/parties", params=params)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert [error["loc"] for error in response.json()["errors"]] == [loc]


async def test_type_role_and_subject_filters(client: AsyncClient, session: AsyncSession):
    maths, physics = await _subject(session, "Mathematics"), await _subject(session, "Physics")
    student = await _person(client, "Sina", "Student")
    tutor = await _person(client, "Tom", "Tutor")
    both = await _person(client, "Bea", "Both")
    await _person(client, "Nora", "None")
    await _company(client, "Musterfirma GmbH")
    await _give_roles(session, student, student=[maths])
    await _give_roles(session, tutor, tutor=[maths, physics])
    await _give_roles(session, both, student=[physics], tutor=[maths])

    assert _names(await _list(client, type="company")) == ["Musterfirma GmbH"]
    assert _names(await _list(client, type="person")) == ["Bea Both", "Nora None", "Sina Student", "Tom Tutor"]
    assert _names(await _list(client, role="student")) == ["Bea Both", "Sina Student"]
    assert _names(await _list(client, role="tutor")) == ["Bea Both", "Tom Tutor"]
    # Without a role the subject counts in any role; with one it is restricted to that role.
    assert _names(await _list(client, subject_id=maths)) == ["Bea Both", "Sina Student", "Tom Tutor"]
    assert _names(await _list(client, subject_id=physics)) == ["Bea Both", "Tom Tutor"]
    assert _names(await _list(client, subject_id=maths, role="student")) == ["Sina Student"]
    assert _names(await _list(client, subject_id=maths, role="tutor")) == ["Bea Both", "Tom Tutor"]
    assert _names(await _list(client, subject_id=physics, role="student")) == ["Bea Both"]
    assert _names(await _list(client, subject_id=physics, role="student", q="tom")) == []


async def test_roles_are_listed_for_a_person_with_none_one_and_both_and_never_for_a_company(
    client: AsyncClient, session: AsyncSession
):
    maths = await _subject(session, "Mathematics")
    await _give_roles(session, await _person(client, "Sina", "Student"), student=[maths])
    await _give_roles(session, await _person(client, "Tom", "Tutor"), tutor=[])
    await _give_roles(session, await _person(client, "Bea", "Both"), student=[], tutor=[maths])
    await _person(client, "Nora", "None")
    await _company(client, "Musterfirma GmbH")
    session.expunge_all()

    page = await _list(client)

    assert {item["display_name"]: (item["type"], item["roles"]) for item in page["items"]} == {
        "Bea Both": ("person", ["student", "tutor"]),
        "Musterfirma GmbH": ("company", []),
        "Nora None": ("person", []),
        "Sina Student": ("person", ["student"]),
        "Tom Tutor": ("person", ["tutor"]),
    }
    assert set(page["items"][0]) == {"id", "type", "display_name", "roles", "updated_at"}


# --- order and paging ---


async def test_items_are_ordered_case_insensitively_across_persons_and_companies(
    client: AsyncClient, statements: list[str]
):
    await _company(client, "beta GmbH")
    await _person(client, "Zoe", "Alpha")
    await _company(client, "Celsius AG")
    await _person(client, "max", "adler")
    await _person(client, "Anna", "adler")
    statements.clear()

    page = await _list(client)

    # Persons sort by last name first: "adler Anna" < "adler max" < "Alpha Zoe" < "beta GmbH" < "Celsius AG".
    assert _names(page) == ["Anna adler", "max adler", "Zoe Alpha", "beta GmbH", "Celsius AG"]
    # The test database collates case-insensitively anyway, so the statement is what proves lower().
    ordered = [statement for statement in statements if " ORDER BY " in statement and "core.party" in statement]
    assert len(ordered) == 1, statements
    assert "ORDER BY lower(coalesce(core.company.name, core.person.lastname || " in ordered[0]
    assert "|| core.person.firstname)), core.party.id" in ordered[0]


async def test_total_counts_the_filtered_set_not_the_page(client: AsyncClient):
    for index in range(5):
        await _person(client, f"Max{index}", "Mustermann")
    await _person(client, "Erika", "Musterfrau")
    await _company(client, "Musterfirma GmbH")

    page = await _list(client, q="mustermann", limit=2)

    assert (len(page["items"]), page["total"], page["limit"], page["offset"]) == (2, 5, 2, 0)
    assert (await _list(client, limit=1))["total"] == 7


async def test_limit_and_offset_page_through_the_order_without_gaps_or_repeats(client: AsyncClient):
    for index in range(7):
        # Identical names: only the unique tie-breaker (party.id) keeps the pages stable.
        await _person(client, "Max", "Mustermann" if index % 2 else "mustermann")
    everything = await _list(client)

    pages = [await _list(client, limit=3, offset=offset) for offset in (0, 3, 6, 9)]

    assert [(page["limit"], page["offset"], page["total"]) for page in pages] == [
        (3, 0, 7),
        (3, 3, 7),
        (3, 6, 7),
        (3, 9, 7),
    ]
    assert [len(page["items"]) for page in pages] == [3, 3, 1, 0]
    paged_ids = [item["id"] for page in pages for item in page["items"]]
    assert paged_ids == [item["id"] for item in everything["items"]]
    assert len(set(paged_ids)) == 7


async def test_the_number_of_statements_does_not_grow_with_the_page_size(
    client: AsyncClient, session: AsyncSession, statements: list[str]
):
    maths, physics = await _subject(session, "Mathematics"), await _subject(session, "Physics")
    for index in range(12):
        person = await _person(client, f"Max{index:02}", "Mustermann", _email(f"max{index}@example.com"))
        await _give_roles(session, person, student=[maths, physics], tutor=[maths])
    session.expunge_all()

    counts = {}
    for limit in (1, 4, 12):
        statements.clear()
        page = await _list(client, limit=limit)
        assert len(page["items"]) == limit
        assert all(item["roles"] == ["student", "tutor"] for item in page["items"])
        counts[limit] = len([statement for statement in statements if statement.startswith("SELECT")])
        session.expunge_all()

    assert counts[1] == counts[4] == counts[12], counts
    assert counts[1] >= 2


# --- updated_since ---

BOUNDARY = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


async def _set_updated_at(session: AsyncSession, party: dict, value: datetime) -> None:
    await session.execute(update(Party).where(Party.id == uuid.UUID(party["id"])).values(updated_at=value))


async def test_updated_since_returns_the_parties_changed_at_or_after_it(client: AsyncClient, session: AsyncSession):
    before = await _person(client, "Anna", "Since-Before")
    boundary = await _person(client, "Berta", "Since-Boundary")
    await _person(client, "Clara", "Since-Now")
    await _set_updated_at(session, before, BOUNDARY - timedelta(microseconds=1))
    await _set_updated_at(session, boundary, BOUNDARY)

    at_the_boundary = await _list(client, q="since-", updated_since=BOUNDARY.isoformat())
    just_after_it = await _list(client, q="since-", updated_since=(BOUNDARY + timedelta(microseconds=1)).isoformat())

    assert _names(at_the_boundary) == ["Berta Since-Boundary", "Clara Since-Now"]
    assert at_the_boundary["total"] == 2
    assert _names(just_after_it) == ["Clara Since-Now"]


async def test_updated_since_reads_any_offset_and_the_z_notation(client: AsyncClient, session: AsyncSession):
    party = await _person(client, "Dora", "Since-Offset")
    await _set_updated_at(session, party, BOUNDARY)

    for same_instant in ("2025-06-01T12:00:00Z", "2025-06-01T14:00:00+02:00"):
        assert _names(await _list(client, q="since-offset", updated_since=same_instant)) == ["Dora Since-Offset"]
    assert _names(await _list(client, q="since-offset", updated_since="2025-06-01T12:00:01Z")) == []


async def test_updated_since_combines_with_the_other_filters(client: AsyncClient, session: AsyncSession):
    old_company = await _company(client, "Since-Combo Alt GmbH")
    await _company(client, "Since-Combo Neu GmbH")
    await _person(client, "Emil", "Since-Combo")
    await _set_updated_at(session, old_company, BOUNDARY - timedelta(days=1))

    page = await _list(client, q="since-combo", type="company", updated_since=BOUNDARY.isoformat())

    assert _names(page) == ["Since-Combo Neu GmbH"]
    assert page["total"] == 1


async def test_updated_since_without_an_offset_is_rejected_for_being_ambiguous(client: AsyncClient):
    """The parameter is known - the 422 is about the missing offset, not about an unknown name."""
    response = await client.get("/parties", params={"updated_since": "2026-01-01T00:00:00"})

    assert response.status_code == 422
    assert [error["type"] for error in response.json()["errors"]] == ["timezone_aware"]


async def test_updated_since_in_the_future_is_an_empty_page_not_an_error(client: AsyncClient):
    await _person(client, "Frieda", "Since-Future")

    page = await _list(client, updated_since=(datetime.now(UTC) + timedelta(days=1)).isoformat())

    assert page["items"] == []
    assert page["total"] == 0


async def test_a_write_anywhere_in_the_aggregate_makes_its_party_appear(client: AsyncClient, backdate):
    """Decision H: a contact info write moves its party, a relation moves the parties on both sides."""
    contacted = await _person(client, "Gustav", "Since-Write")
    parent = await _person(client, "Hanna", "Since-Write")
    child = await _person(client, "Ida", "Since-Write")
    await _person(client, "Jonas", "Since-Write")
    everyone = await _list(client, q="since-write")
    await backdate(*(uuid.UUID(item["id"]) for item in everyone["items"]))
    an_hour_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert _names(await _list(client, q="since-write", updated_since=an_hour_ago)) == []

    added = await client.post(f"/parties/{contacted['id']}/contact-infos", json=_email("gustav@example.com"))
    related = await client.put(f"/parties/{parent['id']}/relations/parent_of/{child['id']}")

    assert (added.status_code, related.status_code) == (201, 200)
    assert _names(await _list(client, q="since-write", updated_since=an_hour_ago)) == [
        "Gustav Since-Write",
        "Hanna Since-Write",
        "Ida Since-Write",
    ]


# --- updated_at: every item carries its party's pull signal (bot-decoupling P0-6) ---


async def test_every_item_carries_the_updated_at_of_its_party(client: AsyncClient, session: AsyncSession):
    """Distinct stamps with a microsecond part: the item neither rounds nor mixes them up, and equals the detail."""
    stamps = {
        "Anna Stamp": datetime(2025, 6, 1, 12, 0, 0, 123456, tzinfo=UTC),
        "Ben Stamp": datetime(2025, 6, 2, 8, 30, 15, 654321, tzinfo=UTC),
        "Stamp GmbH": datetime(2025, 6, 3, 9, 0, 0, 1, tzinfo=UTC),
    }
    for party in [await _person(client, "Anna", "Stamp"), await _person(client, "Ben", "Stamp")]:
        await _set_updated_at(session, party, stamps[party["display_name"]])
    await _set_updated_at(session, await _company(client, "Stamp GmbH"), stamps["Stamp GmbH"])

    page = await _list(client, q="stamp")

    assert {item["display_name"]: datetime.fromisoformat(item["updated_at"]) for item in page["items"]} == stamps
    for item in page["items"]:
        assert item["updated_at"] == (await client.get(f"/parties/{item['id']}")).json()["updated_at"]


async def test_an_items_updated_at_passed_back_as_updated_since_returns_it_again(
    client: AsyncClient, session: AsyncSession
):
    """The cursor of the pull contract: the stamp goes out and comes back without losing its last microsecond."""
    party = await _person(client, "Clara", "Cursor")
    await _set_updated_at(session, party, datetime(2025, 6, 1, 12, 0, 0, 999999, tzinfo=UTC))
    (item,) = (await _list(client, q="cursor"))["items"]

    again = await _list(client, q="cursor", updated_since=item["updated_at"])

    assert [found["id"] for found in again["items"]] == [party["id"]]
