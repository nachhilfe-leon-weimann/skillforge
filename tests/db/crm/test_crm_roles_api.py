"""The student and tutor roles - `PUT` / `DELETE /persons/{party_id}/student|tutor` and the nested create."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import Party, PartyRelation, Student, StudentSubject, Tutor, TutorSubject

pytestmark = pytest.mark.db


@pytest.fixture
async def subjects(client: AsyncClient) -> dict[str, int]:
    created = {}
    for title in ("Mathematics", "physics", "Art"):
        response = await client.post("/subjects", json={"title": title})
        assert response.status_code == 201
        created[title] = response.json()["id"]
    return created


async def _person(client: AsyncClient, **body) -> dict:
    response = await client.post("/persons", json={"firstname": "Max", "lastname": "Mustermann", **body})
    assert response.status_code == 201, response.text
    return response.json()


async def _company(client: AsyncClient) -> dict:
    response = await client.post("/companies", json={"name": "Musterfirma GmbH"})
    assert response.status_code == 201
    return response.json()


async def _count(session: AsyncSession, model, *where) -> int:
    return await session.scalar(select(func.count()).select_from(model).where(*where)) or 0


def _titles(role: dict) -> list[str]:
    return [subject["title"] for subject in role["subjects"]]


# --- PUT ---


async def test_put_student_role_creates_the_role_and_answers_with_the_person_detail(
    client: AsyncClient, subjects: dict[str, int]
):
    person = await _person(client)

    response = await client.put(
        f"/persons/{person['id']}/student",
        json={"preferred_meeting_tool": "microsoft_teams", "subject_ids": [subjects["physics"], subjects["Art"]]},
    )

    assert response.status_code == 200
    detail = response.json()
    assert detail == {
        **person,
        "student": {
            "preferred_meeting_tool": "microsoft_teams",
            "subjects": [{"id": subjects["Art"], "title": "Art"}, {"id": subjects["physics"], "title": "physics"}],
        },
        "updated_at": detail["updated_at"],
    }
    assert (await client.get(f"/parties/{person['id']}")).json() == detail


async def test_put_tutor_role_creates_the_role_and_a_person_can_hold_both(
    client: AsyncClient, subjects: dict[str, int]
):
    person = await _person(client)
    await client.put(f"/persons/{person['id']}/student", json={"preferred_meeting_tool": "discord"})

    response = await client.put(f"/persons/{person['id']}/tutor", json={"subject_ids": [subjects["Mathematics"]]})

    assert response.status_code == 200
    detail = response.json()
    assert detail["student"] == {"preferred_meeting_tool": "discord", "subjects": []}
    assert detail["tutor"] == {"subjects": [{"id": subjects["Mathematics"], "title": "Mathematics"}]}
    listed = (await client.get("/parties", params={"role": "tutor"})).json()["items"]
    assert [(item["id"], item["roles"]) for item in listed] == [(person["id"], ["student", "tutor"])]


@pytest.mark.parametrize("role", ["student", "tutor"])
async def test_the_same_put_twice_answers_200_twice_with_the_same_representation(
    client: AsyncClient, subjects: dict[str, int], backdate, updated_at, role: str
):
    person = await _person(client)
    body = {"subject_ids": [subjects["Mathematics"], subjects["Art"]]}
    if role == "student":
        body["preferred_meeting_tool"] = "in_person"

    first = await client.put(f"/persons/{person['id']}/{role}", json=body)
    # In production the second request is a later transaction: only a timestamp in the past shows
    # whether it counted as a write.
    await backdate(uuid.UUID(person["id"]))
    before = await updated_at(uuid.UUID(person["id"]))
    second = await client.put(f"/persons/{person['id']}/{role}", json=body)

    assert (first.status_code, second.status_code) == (200, 200)
    assert await updated_at(uuid.UUID(person["id"])) == before
    assert {**second.json(), "updated_at": None} == {**first.json(), "updated_at": None}
    assert second.json() == (await client.get(f"/parties/{person['id']}")).json()


async def test_put_replaces_the_data_of_an_existing_role(client: AsyncClient, subjects: dict[str, int]):
    person = await _person(client)
    url = f"/persons/{person['id']}/student"
    await client.put(url, json={"preferred_meeting_tool": "discord", "subject_ids": [subjects["Art"]]})

    tool_only = await client.put(url, json={"preferred_meeting_tool": "phone", "subject_ids": [subjects["Art"]]})
    emptied = await client.put(url, json={"preferred_meeting_tool": "phone"})

    assert tool_only.json()["student"] == {
        "preferred_meeting_tool": "phone",
        "subjects": [{"id": subjects["Art"], "title": "Art"}],
    }
    assert emptied.json()["student"] == {"preferred_meeting_tool": "phone", "subjects": []}


@pytest.mark.parametrize("role", ["student", "tutor"])
async def test_subject_ids_replaces_the_set_by_its_difference(
    client: AsyncClient, session: AsyncSession, subjects: dict[str, int], role: str
):
    person = await _person(client)
    url = f"/persons/{person['id']}/{role}"
    tool = {"preferred_meeting_tool": "discord"} if role == "student" else {}
    await client.put(url, json={**tool, "subject_ids": [subjects["Mathematics"], subjects["physics"]]})
    session.expunge_all()

    async def physics_row() -> str:
        row = await session.execute(
            text(f"SELECT ctid::text FROM core.{role}_subject WHERE subject_id = :id"), {"id": subjects["physics"]}
        )
        return row.scalar_one()

    untouched = await physics_row()

    response = await client.put(url, json={**tool, "subject_ids": [subjects["physics"], subjects["Art"]]})

    assert _titles(response.json()[role]) == ["Art", "physics"]
    # The unchanged row is still the same physical row: it was neither deleted nor re-inserted.
    assert await physics_row() == untouched
    link = StudentSubject if role == "student" else TutorSubject
    stored = (await session.execute(select(link.subject_id))).scalars().all()
    assert sorted(stored) == sorted([subjects["physics"], subjects["Art"]])


@pytest.mark.parametrize("role", ["student", "tutor"])
async def test_a_put_that_only_adds_or_only_removes_subjects_is_a_write(
    client: AsyncClient, subjects: dict[str, int], backdate, updated_at, role: str
):
    person = await _person(client)
    party_id = uuid.UUID(person["id"])
    url = f"/persons/{person['id']}/{role}"
    tool = {"preferred_meeting_tool": "discord"} if role == "student" else {}
    await client.put(url, json={**tool, "subject_ids": [subjects["Art"]]})

    await backdate(party_id)
    before_adding = await updated_at(party_id)
    added = await client.put(url, json={**tool, "subject_ids": [subjects["Art"], subjects["physics"]]})
    after_adding = await updated_at(party_id)

    await backdate(party_id)
    before_removing = await updated_at(party_id)
    removed = await client.put(url, json={**tool, "subject_ids": [subjects["physics"]]})

    assert (_titles(added.json()[role]), _titles(removed.json()[role])) == (["Art", "physics"], ["physics"])
    assert after_adding > before_adding
    assert await updated_at(party_id) > before_removing
    assert _titles((await client.get(f"/parties/{person['id']}")).json()[role]) == ["physics"]


async def test_changing_only_the_meeting_tool_is_a_write(client: AsyncClient, backdate, updated_at):
    person = await _person(client, student={"preferred_meeting_tool": "discord"})
    party_id = uuid.UUID(person["id"])
    await backdate(party_id)
    before = await updated_at(party_id)

    response = await client.put(f"/persons/{person['id']}/student", json={"preferred_meeting_tool": "phone"})

    assert response.json()["student"]["preferred_meeting_tool"] == "phone"
    assert await updated_at(party_id) > before


async def test_duplicate_subject_ids_collapse(client: AsyncClient, subjects: dict[str, int]):
    person = await _person(client)
    maths = subjects["Mathematics"]

    response = await client.put(f"/persons/{person['id']}/tutor", json={"subject_ids": [maths, maths, maths]})

    assert response.status_code == 200
    assert _titles(response.json()["tutor"]) == ["Mathematics"]


@pytest.mark.parametrize("role", ["student", "tutor"])
async def test_an_unknown_subject_is_422_listing_the_unknown_ids_and_nothing_is_written(
    client: AsyncClient, session: AsyncSession, subjects: dict[str, int], backdate, updated_at, role: str
):
    person = await _person(client)
    url = f"/persons/{person['id']}/{role}"
    tool = {"preferred_meeting_tool": "discord"} if role == "student" else {}
    await client.put(url, json={**tool, "subject_ids": [subjects["Art"]]})
    await backdate(uuid.UUID(person["id"]))
    before = (await client.get(f"/parties/{person['id']}")).json()
    # A set of these two iterates as 900008, 900001 - so only sorting puts them in order.
    assert list({900008, 900001}) == [900008, 900001]

    response = await client.put(
        url, json={"preferred_meeting_tool": "phone", "subject_ids": [900008, subjects["physics"], 900001]}
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Unknown subject: 900001, 900008", "code": "unknown_subject"}
    assert (await client.get(f"/parties/{person['id']}")).json() == before


@pytest.mark.parametrize("role", ["student", "tutor"])
async def test_an_unknown_subject_on_a_person_without_the_role_creates_no_role(
    client: AsyncClient, session: AsyncSession, role: str
):
    person = await _person(client)

    response = await client.put(
        f"/persons/{person['id']}/{role}", json={"preferred_meeting_tool": "discord", "subject_ids": [424242]}
    )

    assert (response.status_code, response.json()["code"]) == (422, "unknown_subject")
    assert await _count(session, Student) == 0
    assert await _count(session, Tutor) == 0


@pytest.mark.parametrize(
    ("body", "loc"),
    [
        ({"subject_ids": [0]}, ["body", "subject_ids", 0]),
        ({"subject_ids": [2**31]}, ["body", "subject_ids", 0]),
        ({"subject_ids": ["maths"]}, ["body", "subject_ids", 0]),
        ({"subject_ids": None}, ["body", "subject_ids"]),
        ({"subject_ids": list(range(1, 102))}, ["body", "subject_ids"]),
    ],
    ids=["zero", "beyond the integer range", "not a number", "null", "more than a hundred"],
)
async def test_malformed_subject_ids_are_the_validation_422(client: AsyncClient, body: dict, loc: list):
    person = await _person(client)

    response = await client.put(f"/persons/{person['id']}/tutor", json=body)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert [error["loc"] for error in response.json()["errors"]] == [loc]


async def test_put_student_role_requires_the_meeting_tool(client: AsyncClient):
    person = await _person(client)

    missing = await client.put(f"/persons/{person['id']}/student", json={})
    unknown = await client.put(f"/persons/{person['id']}/student", json={"preferred_meeting_tool": "carrier_pigeon"})

    assert [error["loc"] for error in missing.json()["errors"]] == [["body", "preferred_meeting_tool"]]
    assert [error["loc"] for error in unknown.json()["errors"]] == [["body", "preferred_meeting_tool"]]


# --- DELETE ---


@pytest.mark.parametrize("role", ["student", "tutor"])
async def test_delete_removes_the_role_and_only_that_role(client: AsyncClient, subjects: dict[str, int], role: str):
    person = await _person(
        client,
        student={"preferred_meeting_tool": "discord", "subject_ids": [subjects["Art"]]},
        tutor={"subject_ids": [subjects["physics"]]},
    )
    other = "tutor" if role == "student" else "student"

    response = await client.delete(f"/persons/{person['id']}/{role}")

    assert response.status_code == 204
    assert response.content == b""
    detail = (await client.get(f"/parties/{person['id']}")).json()
    assert detail[role] is None
    assert detail[other] == person[other]


@pytest.mark.parametrize("role", ["student", "tutor"])
async def test_delete_of_a_role_that_is_not_assigned_is_404_role_not_found(client: AsyncClient, role: str):
    person = await _person(client)
    other = "tutor" if role == "student" else "student"
    await client.put(f"/persons/{person['id']}/{other}", json={"preferred_meeting_tool": "discord"})

    first = await client.delete(f"/persons/{person['id']}/{role}")

    assert first.status_code == 404
    assert first.json() == {"detail": "Role not assigned", "code": "role_not_found"}
    assert (await client.delete(f"/persons/{person['id']}/{other}")).status_code == 204
    assert (await client.delete(f"/persons/{person['id']}/{other}")).json()["code"] == "role_not_found"


@pytest.mark.parametrize("role", ["student", "tutor"])
@pytest.mark.parametrize("target", ["company", "unknown"])
async def test_a_company_or_unknown_id_is_404_person_not_found(client: AsyncClient, role: str, target: str):
    party_id = (await _company(client))["id"] if target == "company" else str(uuid.uuid4())
    expected = {"detail": "Person not found", "code": "person_not_found"}

    put = await client.put(f"/persons/{party_id}/{role}", json={"preferred_meeting_tool": "discord"})
    delete = await client.delete(f"/persons/{party_id}/{role}")

    assert (put.status_code, put.json()) == (404, expected)
    assert (delete.status_code, delete.json()) == (404, expected)


RELATIONS = {("anna", "tutor_of", "ben"), ("carl", "tutor_of", "anna"), ("anna", "parent_of", "ben")}


@pytest.mark.parametrize(
    ("role", "removed", "moved"),
    [("tutor", ("anna", "ben"), {"anna", "ben"}), ("student", ("carl", "anna"), {"anna", "carl"})],
    ids=["tutor", "student"],
)
async def test_removing_a_role_removes_the_tutor_of_it_anchored_and_moves_both_sides(
    client: AsyncClient,
    session: AsyncSession,
    statements: list[str],
    backdate,
    updated_at,
    role: str,
    removed: tuple[str, str],
    moved: set[str],
):
    """Decision O of bot-decoupling: a `tutor_of` goes with either of its roles, and nothing else changes.

    Anna tutors Ben, is tutored by Carl and is Ben's parent. Taking one of her roles away removes the `tutor_of` on
    that side only; only its two parties move. CRM rules only (ADR 0007): no statement reads Discord or `ext` state.
    """
    anna = await _person(client, firstname="Anna", student={"preferred_meeting_tool": "discord"}, tutor={})
    ben = await _person(client, firstname="Ben", student={"preferred_meeting_tool": "discord"})
    carl = await _person(client, firstname="Carl", tutor={})
    ids = {name: uuid.UUID(party["id"]) for name, party in (("anna", anna), ("ben", ben), ("carl", carl))}
    for from_name, relation_type, to_name in sorted(RELATIONS):
        response = await client.put(f"/parties/{ids[from_name]}/relations/{relation_type}/{ids[to_name]}")
        assert response.status_code == 200, response.text
    await backdate(*ids.values())
    before = {name: await updated_at(party_id) for name, party_id in ids.items()}
    statements.clear()

    response = await client.delete(f"/persons/{anna['id']}/{role}")
    during_the_request = list(statements)  # the checks below query the same connection

    assert response.status_code == 204
    names = {party_id: name for name, party_id in ids.items()}
    stored = await session.execute(select(PartyRelation.from_party_id, PartyRelation.type, PartyRelation.to_party_id))
    assert {
        (names[from_id], relation_type.value, names[to_id]) for from_id, relation_type, to_id in stored.tuples()
    } == (RELATIONS - {(removed[0], "tutor_of", removed[1])})
    assert {name for name, party_id in ids.items() if await updated_at(party_id) != before[name]} == moved
    assert [statement for statement in during_the_request if " bot." in statement or " ext." in statement] == []


# --- nested create ---


async def test_post_persons_creates_both_roles_in_the_same_call(client: AsyncClient, subjects: dict[str, int]):
    response = await client.post(
        "/persons",
        json={
            "firstname": "Max",
            "lastname": "Mustermann",
            "student": {"preferred_meeting_tool": "in_person", "subject_ids": [subjects["physics"], subjects["Art"]]},
            "tutor": {"subject_ids": [subjects["Mathematics"]]},
        },
    )

    assert response.status_code == 201
    detail = response.json()
    assert detail["student"]["preferred_meeting_tool"] == "in_person"
    assert _titles(detail["student"]) == ["Art", "physics"]
    assert _titles(detail["tutor"]) == ["Mathematics"]
    assert (await client.get(response.headers["location"].replace("/api/v1/crm", ""))).json() == detail


async def test_post_persons_with_a_null_or_absent_role_creates_none(client: AsyncClient):
    explicit = await _person(client, student=None, tutor=None)
    absent = await _person(client)

    assert (explicit["student"], explicit["tutor"], absent["student"], absent["tutor"]) == (None, None, None, None)


async def test_post_persons_with_an_unknown_subject_creates_no_party_at_all(
    client: AsyncClient, session: AsyncSession, subjects: dict[str, int]
):
    response = await client.post(
        "/persons",
        json={
            "firstname": "Max",
            "lastname": "Mustermann",
            "contact_infos": [{"type": "email", "value": "max@example.com"}],
            "student": {"preferred_meeting_tool": "discord", "subject_ids": [subjects["Art"], 900008]},
            "tutor": {"subject_ids": [900001, subjects["physics"]]},
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Unknown subject: 900001, 900008", "code": "unknown_subject"}
    assert await _count(session, Party) == 0
    assert await _count(session, Student) == 0
    assert await _count(session, Tutor) == 0
