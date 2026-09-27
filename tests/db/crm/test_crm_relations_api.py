"""`/parties/{party_id}/relations` and the four-call reference flow, against the real database."""

import uuid
from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import PartyRelation

pytestmark = pytest.mark.db

TYPES = ["parent_of", "tutor_of", "pays_for"]


async def _person(client: AsyncClient, firstname: str, **body) -> dict:
    response = await client.post("/persons", json={"firstname": firstname, "lastname": "Mustermann", **body})
    assert response.status_code == 201, response.text
    return response.json()


async def _company(client: AsyncClient, name: str = "Musterfirma GmbH") -> dict:
    response = await client.post("/companies", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


async def _student(client: AsyncClient, firstname: str = "Mia") -> dict:
    return await _person(client, firstname, student={"preferred_meeting_tool": "discord"})


async def _tutor(client: AsyncClient, firstname: str = "Tom") -> dict:
    return await _person(client, firstname, tutor={})


async def _put(client: AsyncClient, from_party: dict, type: str, to_party: dict):
    return await client.put(f"/parties/{from_party['id']}/relations/{type}/{to_party['id']}")


async def _relations(client: AsyncClient, party: dict, **params) -> dict:
    response = await client.get(f"/parties/{party['id']}/relations", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def _relation_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(PartyRelation)) or 0


def _item(party: dict, roles: list[str]) -> dict:
    """The list item of ``party``. A test runs in one transaction, so every write stamps the ``updated_at`` it had."""
    return {
        "id": party["id"],
        "type": party["type"],
        "display_name": party["display_name"],
        "roles": roles,
        "updated_at": party["updated_at"],
    }


# --- the rules ---


async def test_put_answers_200_with_the_relation_seen_from_the_party_in_the_path(client: AsyncClient):
    parent, child = await _person(client, "Erika"), await _student(client)

    response = await _put(client, parent, "parent_of", child)

    assert response.status_code == 200
    assert response.json() == {
        "type": "parent_of",
        "direction": "outgoing",
        "party": _item(child, ["student"]),
        "created_at": response.json()["created_at"],
    }


@pytest.mark.parametrize(
    ("type", "from_kind", "to_kind"),
    [
        ("parent_of", "person", "person"),
        ("parent_of", "tutor", "student"),
        ("tutor_of", "tutor", "student"),
        ("tutor_of", "both", "both"),
        ("pays_for", "person", "person"),
        ("pays_for", "company", "student"),
    ],
)
async def test_a_pair_that_follows_the_rule_is_accepted(client: AsyncClient, type: str, from_kind: str, to_kind: str):
    from_party, to_party = await _party_of(client, from_kind, "Erika"), await _party_of(client, to_kind, "Mia")

    response = await _put(client, from_party, type, to_party)

    assert response.status_code == 200, response.text
    assert (response.json()["type"], response.json()["party"]["id"]) == (type, to_party["id"])


@pytest.mark.parametrize(
    ("type", "from_kind", "to_kind", "rule"),
    [
        ("parent_of", "company", "person", "parent_of must start at a person"),
        ("parent_of", "person", "company", "parent_of must point to a person"),
        ("tutor_of", "person", "student", "tutor_of must start at a person holding the tutor role"),
        ("tutor_of", "student", "student", "tutor_of must start at a person holding the tutor role"),
        ("tutor_of", "company", "student", "tutor_of must start at a person holding the tutor role"),
        ("tutor_of", "tutor", "person", "tutor_of must point to a person holding the student role"),
        ("tutor_of", "tutor", "tutor", "tutor_of must point to a person holding the student role"),
        ("tutor_of", "tutor", "company", "tutor_of must point to a person holding the student role"),
        ("pays_for", "person", "company", "pays_for must point to a person"),
        ("pays_for", "company", "company", "pays_for must point to a person"),
    ],
)
async def test_a_pair_that_breaks_the_rule_is_422_naming_the_rule_and_writes_nothing(
    client: AsyncClient, session: AsyncSession, type: str, from_kind: str, to_kind: str, rule: str
):
    from_party, to_party = await _party_of(client, from_kind, "Erika"), await _party_of(client, to_kind, "Mia")

    response = await _put(client, from_party, type, to_party)

    assert response.status_code == 422
    assert response.json() == {"detail": f"Invalid party relation: {rule}", "code": "invalid_party_relation"}
    assert await _relation_count(session) == 0


@pytest.mark.parametrize("type", TYPES)
async def test_a_party_cannot_be_related_to_itself_whatever_the_type(
    client: AsyncClient, session: AsyncSession, type: str
):
    both = await _party_of(client, "both", "Bea")

    response = await _put(client, both, type, both)

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Invalid party relation: a party cannot be related to itself",
        "code": "invalid_party_relation",
    }
    assert await _relation_count(session) == 0


async def _party_of(client: AsyncClient, kind: str, firstname: str) -> dict:
    match kind:
        case "person":
            return await _person(client, firstname)
        case "student":
            return await _student(client, firstname)
        case "tutor":
            return await _tutor(client, firstname)
        case "both":
            return await _person(client, firstname, student={"preferred_meeting_tool": "discord"}, tutor={})
        case "company":
            return await _company(client, f"{firstname} GmbH")
    raise AssertionError(kind)


# --- missing parties, idempotency, delete ---


@pytest.mark.parametrize("type", TYPES)
async def test_an_unknown_party_is_404_party_not_found_and_an_unknown_other_side_related_party_not_found(
    client: AsyncClient, type: str
):
    known = await _party_of(client, "both", "Bea")
    unknown = {"id": str(uuid.uuid4())}

    unknown_from = await _put(client, unknown, type, known)
    unknown_to = await _put(client, known, type, unknown)
    both_unknown = await _put(client, unknown, type, {"id": str(uuid.uuid4())})

    assert (unknown_from.status_code, unknown_from.json()) == (
        404,
        {"detail": "Party not found", "code": "party_not_found"},
    )
    assert (unknown_to.status_code, unknown_to.json()) == (
        404,
        {"detail": "Related party not found", "code": "related_party_not_found"},
    )
    assert both_unknown.json()["code"] == "party_not_found"


async def test_put_is_idempotent_and_the_second_one_is_not_a_write(
    client: AsyncClient, session: AsyncSession, backdate, updated_at
):
    parent, child = await _person(client, "Erika"), await _person(client, "Mia")
    first = await _put(client, parent, "parent_of", child)
    ids = (uuid.UUID(parent["id"]), uuid.UUID(child["id"]))
    await backdate(*ids)
    before = [await updated_at(party_id) for party_id in ids]

    second = await _put(client, parent, "parent_of", child)

    assert (first.status_code, second.status_code) == (200, 200)
    first_body, second_body = first.json(), second.json()
    # The same answer but for the other side's stamp: the backdate moved it, and the repeat - no write - left it there.
    assert datetime.fromisoformat(second_body["party"].pop("updated_at")) == before[1]
    del first_body["party"]["updated_at"]
    assert second_body == first_body
    assert [await updated_at(party_id) for party_id in ids] == before
    assert await _relation_count(session) == 1


async def test_the_same_pair_may_be_related_by_several_types_and_in_both_directions(
    client: AsyncClient, session: AsyncSession
):
    parent, child = await _party_of(client, "both", "Erika"), await _party_of(client, "both", "Mia")

    statuses = [
        (await _put(client, parent, "parent_of", child)).status_code,
        (await _put(client, parent, "pays_for", child)).status_code,
        (await _put(client, parent, "tutor_of", child)).status_code,
        (await _put(client, child, "tutor_of", parent)).status_code,
    ]

    assert statuses == [200, 200, 200, 200]
    assert await _relation_count(session) == 4


async def test_delete_answers_204_and_a_missing_relation_is_404(client: AsyncClient, session: AsyncSession):
    parent, child = await _person(client, "Erika"), await _person(client, "Mia")
    await _put(client, parent, "parent_of", child)
    await _put(client, parent, "pays_for", child)
    url = f"/parties/{parent['id']}/relations/parent_of/{child['id']}"
    not_found = {"detail": "Party relation not found", "code": "party_relation_not_found"}

    reversed_direction = await client.delete(f"/parties/{child['id']}/relations/parent_of/{parent['id']}")
    other_type = await client.delete(f"/parties/{parent['id']}/relations/tutor_of/{child['id']}")
    unknown_parties = await client.delete(f"/parties/{uuid.uuid4()}/relations/parent_of/{uuid.uuid4()}")
    deleted = await client.delete(url)
    again = await client.delete(url)

    for response in (reversed_direction, other_type, unknown_parties, again):
        assert (response.status_code, response.json()) == (404, not_found)
    assert (deleted.status_code, deleted.content) == (204, b"")
    assert [(item["type"]) for item in (await _relations(client, parent))["items"]] == ["pays_for"]
    assert await _relation_count(session) == 1


@pytest.mark.parametrize("write", ["put", "delete"])
async def test_a_relation_write_moves_updated_at_of_both_parties_and_of_no_other(
    client: AsyncClient, backdate, updated_at, write: str
):
    parent, child, bystander = [await _person(client, name) for name in ("Erika", "Mia", "Nora")]
    if write == "delete":
        await _put(client, parent, "parent_of", child)
    ids = [uuid.UUID(party["id"]) for party in (parent, child, bystander)]
    await backdate(*ids)
    before = [await updated_at(party_id) for party_id in ids]

    url = f"/parties/{parent['id']}/relations/parent_of/{child['id']}"
    response = await (client.put(url) if write == "put" else client.delete(url))

    assert response.status_code in (200, 204)
    after = [await updated_at(party_id) for party_id in ids]
    assert after[0] > before[0] and after[1] > before[1]
    assert after[2] == before[2]


# --- GET ---


async def test_get_returns_both_directions_and_the_child_sees_parent_of_as_incoming(client: AsyncClient):
    mother, child, tutor = await _person(client, "Erika"), await _student(client), await _tutor(client)
    await _put(client, mother, "parent_of", child)
    await _put(client, tutor, "tutor_of", child)
    await _put(client, child, "pays_for", tutor)

    from_child = await _relations(client, child)

    assert (from_child["total"], from_child["limit"], from_child["offset"]) == (3, 50, 0)
    seen = {(item["type"], item["direction"]): item["party"] for item in from_child["items"]}
    assert seen == {
        ("parent_of", "incoming"): _item(mother, []),
        ("tutor_of", "incoming"): _item(tutor, ["tutor"]),
        ("pays_for", "outgoing"): _item(tutor, ["tutor"]),
    }
    assert all(item["created_at"] for item in from_child["items"])
    from_mother = await _relations(client, mother)
    assert [(item["type"], item["direction"], item["party"]) for item in from_mother["items"]] == [
        ("parent_of", "outgoing", _item(child, ["student"]))
    ]


async def test_get_filters_by_direction_and_type(client: AsyncClient):
    mother, child, tutor = await _person(client, "Erika"), await _student(client), await _tutor(client)
    await _put(client, mother, "parent_of", child)
    await _put(client, mother, "pays_for", child)
    await _put(client, tutor, "tutor_of", child)
    await _put(client, child, "pays_for", tutor)

    def keys(page: dict) -> set[tuple[str, str]]:
        return {(item["type"], item["direction"]) for item in page["items"]}

    incoming = await _relations(client, child, direction="incoming")
    assert keys(incoming) == {("parent_of", "incoming"), ("pays_for", "incoming"), ("tutor_of", "incoming")}
    assert incoming["total"] == 3
    assert keys(await _relations(client, child, direction="outgoing")) == {("pays_for", "outgoing")}
    assert keys(await _relations(client, child, type="pays_for")) == {
        ("pays_for", "incoming"),
        ("pays_for", "outgoing"),
    }
    assert keys(await _relations(client, child, type="pays_for", direction="incoming")) == {("pays_for", "incoming")}
    assert await _relations(client, child, type="parent_of", direction="outgoing") == {
        "items": [],
        "total": 0,
        "limit": 50,
        "offset": 0,
    }


async def test_get_orders_by_creation_type_and_other_party_and_pages_without_gaps(
    client: AsyncClient, statements: list[str]
):
    payer = await _company(client)
    children = [await _person(client, f"Kind{index}") for index in range(5)]
    for child in children:
        await _put(client, payer, "pays_for", child)
    everything = await _relations(client, payer)
    statements.clear()

    pages = [await _relations(client, payer, limit=2, offset=offset) for offset in (0, 2, 4, 6)]

    assert [(page["limit"], page["offset"], page["total"]) for page in pages] == [
        (2, 0, 5),
        (2, 2, 5),
        (2, 4, 5),
        (2, 6, 5),
    ]
    paged = [item["party"]["id"] for page in pages for item in page["items"]]
    assert paged == [item["party"]["id"] for item in everything["items"]]
    assert sorted(paged) == sorted(child["id"] for child in children)
    # Equal created_at and type, so the other party's ID decides (uuid order is its hex string order).
    assert paged == sorted(paged)
    # One transaction means one created_at here, so the order itself is asserted on the statement.
    ordered = [
        statement for statement in statements if "FROM core.party_relation" in statement and "ORDER BY" in statement
    ]
    assert len(ordered) == 4
    order_by = ordered[0].split("ORDER BY", 1)[1]
    assert order_by.strip().startswith(
        "core.party_relation.created_at, core.party_relation.type, CASE WHEN (core.party_relation.from_party_id = $"
    )
    assert "THEN core.party_relation.to_party_id ELSE core.party_relation.from_party_id END" in order_by


async def test_the_number_of_statements_of_a_relation_page_does_not_grow_with_its_size(
    client: AsyncClient, session: AsyncSession, statements: list[str]
):
    payer = await _company(client)
    for index in range(8):
        await _put(client, payer, "pays_for", await _student(client, f"Kind{index}"))
    session.expunge_all()

    counts = {}
    for limit in (1, 8):
        statements.clear()
        page = await _relations(client, payer, limit=limit)
        assert len(page["items"]) == limit
        assert all(item["party"]["roles"] == ["student"] for item in page["items"])
        counts[limit] = len([statement for statement in statements if statement.startswith("SELECT")])
        session.expunge_all()

    assert counts[1] == counts[8], counts


async def test_get_of_an_unknown_party_is_404_and_a_party_without_relations_an_empty_page(client: AsyncClient):
    lonely = await _person(client, "Nora")

    unknown = await client.get(f"/parties/{uuid.uuid4()}/relations")

    assert (unknown.status_code, unknown.json()) == (404, {"detail": "Party not found", "code": "party_not_found"})
    assert await _relations(client, lonely) == {"items": [], "total": 0, "limit": 50, "offset": 0}


@pytest.mark.parametrize(
    ("path", "loc"),
    [
        ("/relations/married_to/{other}", ["path", "type"]),
        ("/relations/parent_of/not-a-uuid", ["path", "to_party_id"]),
        ("/relations?direction=sideways", ["query", "direction"]),
        ("/relations?typ=parent_of", ["query", "typ"]),
    ],
    ids=["unknown type", "malformed other side", "unknown direction", "unknown parameter"],
)
async def test_a_malformed_relation_request_is_the_validation_422(client: AsyncClient, path: str, loc: list):
    party = await _person(client, "Erika")
    url = f"/parties/{party['id']}" + path.format(other=uuid.uuid4())

    response = await (client.get(url) if "?" in path else client.put(url))

    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["errors"]] == [loc]


# --- end to end ---


async def test_the_reference_flow_enters_a_student_with_a_paying_mother_in_four_calls(client: AsyncClient):
    """Goal 1 of the CRM API spec: four calls, no database access."""
    maths = (await client.post("/subjects", json={"title": "Mathematics"})).json()

    # 1 - the student, with contact infos and the student role
    student_response = await client.post(
        "/persons",
        json={
            "firstname": "Mia",
            "lastname": "Mustermann",
            "contact_infos": [{"type": "email", "value": "Mia@Example.com", "label": "school"}],
            "student": {"preferred_meeting_tool": "discord", "subject_ids": [maths["id"]]},
        },
    )
    student = student_response.json()
    # 2 - the mother
    mother_response = await client.post(
        "/persons",
        json={
            "firstname": "Erika",
            "lastname": "Mustermann",
            "contact_infos": [
                {"type": "email", "value": "erika@example.com"},
                {"type": "phone", "value": "+49 151 234 567", "label": "mobile"},
            ],
        },
    )
    mother = mother_response.json()
    # 3 and 4 - the mother is the parent and pays
    parent_of = await client.put(f"/parties/{mother['id']}/relations/parent_of/{student['id']}")
    pays_for = await client.put(f"/parties/{mother['id']}/relations/pays_for/{student['id']}")

    assert [r.status_code for r in (student_response, mother_response, parent_of, pays_for)] == [201, 201, 200, 200]
    assert parent_of.json()["party"]["display_name"] == "Mia Mustermann"

    detail = (await client.get(f"/parties/{student['id']}")).json()
    assert detail["type"] == "person"
    assert detail["display_name"] == "Mia Mustermann"
    assert detail["student"] == {"preferred_meeting_tool": "discord", "subjects": [maths]}
    assert detail["tutor"] is None
    assert [(info["type"], info["value"], info["label"]) for info in detail["contact_infos"]] == [
        ("email", "mia@example.com", "school")
    ]

    relations = (await client.get(f"/parties/{student['id']}/relations")).json()
    assert relations["total"] == 2
    assert {(item["type"], item["direction"]) for item in relations["items"]} == {
        ("parent_of", "incoming"),
        ("pays_for", "incoming"),
    }
    assert {item["party"]["id"] for item in relations["items"]} == {mother["id"]}
    assert relations["items"][0]["party"] == _item(mother, [])

    found = (await client.get("/parties", params={"q": "erika@example"})).json()
    assert [item["id"] for item in found["items"]] == [mother["id"]]
    students = (await client.get("/parties", params={"role": "student", "subject_id": maths["id"]})).json()
    assert [item["id"] for item in students["items"]] == [student["id"]]
