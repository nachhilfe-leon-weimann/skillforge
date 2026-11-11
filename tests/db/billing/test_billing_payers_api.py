"""The payer routes and the payer a lesson stores (billing spec, SF3-1 and PD1), on Postgres."""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import ANY

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import Lesson, ManualPayer, PartyRelationType, SevdeskContact
from app.services.crm import companies, persons, relations

pytestmark = pytest.mark.db

START = "2026-11-02T16:00:00+01:00"
CONTACT = "4711001"
OTHER_CONTACT = "4711002"


async def _add(client: AsyncClient, student: uuid.UUID, **fields: Any) -> dict[str, Any]:
    response = await client.post(
        "/lessons", json={"student_id": str(student), "subject": "Mathe", "starts_at": START, "minutes": 60, **fields}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _patch(client: AsyncClient, lesson: dict[str, Any], **fields: Any) -> dict[str, Any]:
    response = await client.patch(f"/lessons/{lesson['id']}", json=fields)
    assert response.status_code == 200, response.text
    return response.json()


async def _payer(session: AsyncSession, lesson: dict[str, Any]) -> uuid.UUID | None:
    return await session.scalar(
        select(Lesson.payer_party_id)
        .where(Lesson.id == uuid.UUID(lesson["id"]))
        .execution_options(populate_existing=True)
    )


async def _parent(session: AsyncSession, firstname: str = "Parent") -> uuid.UUID:
    return (await persons.create_person(session, firstname=firstname, lastname="A")).id


async def _pays_for(session: AsyncSession, payer: uuid.UUID, student: uuid.UUID) -> None:
    await relations.put_relation(session, payer, PartyRelationType.PAYS_FOR, student)


async def _stops_paying(session: AsyncSession, payer: uuid.UUID, student: uuid.UUID) -> None:
    await relations.remove_relation(session, payer, PartyRelationType.PAYS_FOR, student)


def _error(response) -> str:
    return response.json()["code"]


async def _row_version(session: AsyncSession, table: str, party: uuid.UUID) -> str | None:
    """The physical place of the row: any UPDATE writes a new version elsewhere, even within one transaction, where
    ``now()`` - and so ``updated_at`` - stands still."""
    return await session.scalar(text(f"SELECT ctid::text FROM {table} WHERE party_id = :party"), {"party": party})


# --- The payer a lesson stores (PD1) ---


async def test_the_tick_stores_the_payer(client: AsyncClient, session: AsyncSession, student: uuid.UUID):
    parent = await _parent(session)
    await _pays_for(session, parent, student)
    lesson = await _add(client, student)
    assert await _payer(session, lesson) is None

    await _patch(client, lesson, status="held")

    assert await _payer(session, lesson) == parent


async def test_a_student_without_pays_for_is_stored_as_the_payer(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID
):
    lesson = await _add(client, student)

    await _patch(client, lesson, status="held")

    assert await _payer(session, lesson) == student


async def test_a_company_that_pays_is_stored_as_the_payer(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID
):
    authority = await companies.create_company(session, name="Authority C")
    await _pays_for(session, authority.id, student)
    lesson = await _add(client, student)

    await _patch(client, lesson, status="held")

    assert await _payer(session, lesson) == authority.id


async def test_a_late_cancellation_gets_its_payer_when_it_is_marked_billable(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID
):
    parent = await _parent(session)
    await _pays_for(session, parent, student)
    lesson = await _add(client, student)

    await _patch(client, lesson, status="cancelled")
    assert await _payer(session, lesson) is None

    await _patch(client, lesson, billable=True)
    assert await _payer(session, lesson) == parent


async def test_a_tick_with_several_payers_is_not_refused_and_stores_no_payer(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID
):
    """Nothing picks one of two payers: the preview blocks a billable lesson without a payer (SF5-4)."""
    await _pays_for(session, await _parent(session), student)
    await _pays_for(session, await _parent(session, "Other"), student)
    lesson = await _add(client, student)

    ticked = await _patch(client, lesson, status="held")

    assert (ticked["status"], ticked["billable"]) == ("held", True)
    assert await _payer(session, lesson) is None


async def test_a_billable_lesson_keeps_its_payer_through_unrelated_edits_when_pays_for_changes(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID
):
    old, new = await _parent(session), await _parent(session, "New")
    await _pays_for(session, old, student)
    lesson = await _add(client, student)
    await _patch(client, lesson, status="held")

    await _stops_paying(session, old, student)
    await _pays_for(session, new, student)
    # Every edit that is not the student, the day or becoming billable: a repeated tick, note, subject, minutes,
    # own rate, and a later start on the same Berlin day.
    for fields in (
        {"status": "held"},
        {"note": "extra"},
        {"subject": "Physik"},
        {"minutes": 45},
        {"rate_cents_per_hour": 2000},
        {"starts_at": "2026-11-02T23:30:00+01:00"},
    ):
        await _patch(client, lesson, **fields)
        assert await _payer(session, lesson) == old, fields


async def test_becoming_billable_again_resolves_the_payer_afresh(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID
):
    old, new = await _parent(session), await _parent(session, "New")
    await _pays_for(session, old, student)
    lesson = await _add(client, student)
    await _patch(client, lesson, status="held")
    await _stops_paying(session, old, student)
    await _pays_for(session, new, student)

    # Goodwill drops the payer with the flag; switching it back on resolves the payer of today.
    await _patch(client, lesson, billable=False)
    assert await _payer(session, lesson) is None
    await _patch(client, lesson, billable=True)
    assert await _payer(session, lesson) == new


@pytest.mark.parametrize("status", ["planned", "not_held", "cancelled"])
async def test_a_lesson_that_stops_being_billable_drops_its_payer(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID, status: str
):
    lesson = await _add(client, student)
    await _patch(client, lesson, status="held")

    await _patch(client, lesson, status=status)

    assert await _payer(session, lesson) is None


async def test_changing_the_student_resolves_the_payer_afresh(
    client: AsyncClient,
    session: AsyncSession,
    student: uuid.UUID,
    new_student: Callable[[], Awaitable[uuid.UUID]],
):
    sibling = await new_student()
    parent = await _parent(session)
    await _pays_for(session, parent, sibling)
    lesson = await _add(client, student)
    await _patch(client, lesson, status="held")
    assert await _payer(session, lesson) == student

    await _patch(client, lesson, student_id=str(sibling))

    assert await _payer(session, lesson) == parent


async def test_changing_the_berlin_day_resolves_the_payer_afresh(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID
):
    lesson = await _add(client, student)
    await _patch(client, lesson, status="held")
    parent = await _parent(session)
    await _pays_for(session, parent, student)

    # 00:30 in Berlin is the next Berlin day, though still the same day in UTC.
    await _patch(client, lesson, starts_at="2026-11-03T00:30:00+01:00")

    assert await _payer(session, lesson) == parent


# --- POST /lessons/{lesson_id}/resolve-payer ---


async def test_resolve_payer_stores_the_payer_of_today(client: AsyncClient, session: AsyncSession, student: uuid.UUID):
    lesson = await _add(client, student)
    await _patch(client, lesson, status="held")
    authority = await companies.create_company(session, name="Authority C")
    await _pays_for(session, authority.id, student)

    response = await client.post(f"/lessons/{lesson['id']}/resolve-payer")

    assert response.status_code == 200
    assert response.json() == {
        "lesson_id": lesson["id"],
        "student_id": str(student),
        "billable": True,
        "payer_id": str(authority.id),
    }
    assert await _payer(session, lesson) == authority.id


async def test_resolve_payer_with_several_payers_drops_the_stored_one(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID
):
    lesson = await _add(client, student)
    await _patch(client, lesson, status="held")
    await _pays_for(session, await _parent(session), student)
    await _pays_for(session, await _parent(session, "Other"), student)

    response = await client.post(f"/lessons/{lesson['id']}/resolve-payer")

    assert response.status_code == 200
    assert response.json()["payer_id"] is None
    assert await _payer(session, lesson) is None


async def test_resolve_payer_leaves_a_lesson_that_is_not_billable_without_a_payer(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID
):
    lesson = await _add(client, student)

    response = await client.post(f"/lessons/{lesson['id']}/resolve-payer")

    assert response.status_code == 200
    assert (response.json()["billable"], response.json()["payer_id"]) == (False, None)
    assert await _payer(session, lesson) is None


async def test_resolve_payer_of_a_missing_lesson_is_lesson_not_found(client: AsyncClient):
    response = await client.post(f"/lessons/{uuid.uuid4()}/resolve-payer")

    assert response.status_code == 404
    assert _error(response) == "lesson_not_found"


# --- GET /students/{party_id}/payer ---


async def test_the_student_payer_names_the_payer_its_contact_and_its_mark(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID
):
    parent = await _parent(session)
    await _pays_for(session, parent, student)
    url = f"/students/{student}/payer"
    assert (await client.get(url)).json() == {
        "student_id": str(student),
        "payer_id": str(parent),
        "ambiguous": False,
        "sevdesk_contact_linked": False,
        "manual_billing": False,
    }

    await client.put(f"/parties/{parent}/sevdesk-contact", json={"sevdesk_contact_id": CONTACT})
    await client.put(f"/parties/{parent}/manual-billing", json={})
    payer = (await client.get(url)).json()

    assert (payer["sevdesk_contact_linked"], payer["manual_billing"]) == (True, True)
    assert CONTACT not in str(payer)


async def test_the_student_payer_is_the_student_without_pays_for(client: AsyncClient, student: uuid.UUID):
    payer = (await client.get(f"/students/{student}/payer")).json()

    assert (payer["payer_id"], payer["ambiguous"]) == (str(student), False)


async def test_the_student_payer_reports_several_payers_as_ambiguous(
    client: AsyncClient, session: AsyncSession, student: uuid.UUID
):
    for firstname in ("Parent", "Other"):
        payer = await _parent(session, firstname)
        await _pays_for(session, payer, student)
        await client.put(f"/parties/{payer}/manual-billing", json={})

    assert (await client.get(f"/students/{student}/payer")).json() == {
        "student_id": str(student),
        "payer_id": None,
        "ambiguous": True,
        "sevdesk_contact_linked": False,
        "manual_billing": False,
    }


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("GET", "students/{party_id}/payer", None),
        ("PUT", "parties/{party_id}/sevdesk-contact", {"sevdesk_contact_id": CONTACT}),
        ("DELETE", "parties/{party_id}/sevdesk-contact", None),
        ("PUT", "parties/{party_id}/manual-billing", {}),
        ("DELETE", "parties/{party_id}/manual-billing", None),
    ],
)
async def test_a_party_that_does_not_exist_is_party_not_found(
    client: AsyncClient, method: str, suffix: str, body: dict[str, Any] | None
):
    response = await client.request(method, "/" + suffix.format(party_id=uuid.uuid4()), json=body)

    assert response.status_code == 404
    assert _error(response) == "party_not_found"


# --- PUT and DELETE /parties/{party_id}/sevdesk-contact ---


async def _contacts(session: AsyncSession) -> list[tuple[str, uuid.UUID]]:
    rows = await session.execute(select(SevdeskContact.sevdesk_id, SevdeskContact.party_id))
    return sorted((sevdesk_id, party_id) for sevdesk_id, party_id in rows)


async def test_put_links_the_contact_and_again_changes_nothing(client: AsyncClient, session: AsyncSession):
    party = await _parent(session)
    url = f"/parties/{party}/sevdesk-contact"

    first = await client.put(url, json={"sevdesk_contact_id": f" {CONTACT} "})
    version = await _row_version(session, "ext.sevdesk_contact", party)
    again = await client.put(url, json={"sevdesk_contact_id": CONTACT})

    assert first.status_code == again.status_code == 200
    assert first.json() == {"party_id": str(party), "sevdesk_contact_id": CONTACT, "created_at": ANY}
    assert again.json() == first.json()
    assert await _row_version(session, "ext.sevdesk_contact", party) == version
    assert await _contacts(session) == [(CONTACT, party)]


async def test_a_contact_linked_to_another_party_is_already_linked(client: AsyncClient, session: AsyncSession):
    party, other = await _parent(session), await _parent(session, "Other")
    await client.put(f"/parties/{party}/sevdesk-contact", json={"sevdesk_contact_id": CONTACT})

    response = await client.put(f"/parties/{other}/sevdesk-contact", json={"sevdesk_contact_id": CONTACT})

    assert response.status_code == 409
    assert response.json() == {
        "detail": "The party or the sevDesk contact is already linked",
        "code": "sevdesk_contact_already_linked",
    }
    assert await _contacts(session) == [(CONTACT, party)]


async def test_a_second_contact_for_one_party_is_already_linked(client: AsyncClient, session: AsyncSession):
    party = await _parent(session)
    url = f"/parties/{party}/sevdesk-contact"
    await client.put(url, json={"sevdesk_contact_id": CONTACT})

    response = await client.put(url, json={"sevdesk_contact_id": OTHER_CONTACT})

    assert response.status_code == 409
    assert _error(response) == "sevdesk_contact_already_linked"
    assert OTHER_CONTACT not in response.text and CONTACT not in response.text
    assert await _contacts(session) == [(CONTACT, party)]


async def test_delete_unlinks_and_a_party_without_a_link_answers_204_too(client: AsyncClient, session: AsyncSession):
    party = await _parent(session)
    url = f"/parties/{party}/sevdesk-contact"
    await client.put(url, json={"sevdesk_contact_id": CONTACT})

    assert (await client.delete(url)).status_code == 204
    assert await _contacts(session) == []
    assert (await client.delete(url)).status_code == 204

    # Unlinked, the party takes another contact.
    assert (await client.put(url, json={"sevdesk_contact_id": OTHER_CONTACT})).status_code == 200


@pytest.mark.parametrize("contact_id", ["1", "4711", "1234567890123456789"])
async def test_a_contact_id_in_its_canonical_form_is_linked(
    client: AsyncClient, session: AsyncSession, contact_id: str
):
    party = await _parent(session)

    response = await client.put(f"/parties/{party}/sevdesk-contact", json={"sevdesk_contact_id": contact_id})

    assert response.status_code == 200
    assert await _contacts(session) == [(contact_id, party)]


@pytest.mark.parametrize("contact_id", ["", " ", "12a4", "-1", "0", "04711", "00", "1" * 65, 4711])
async def test_a_contact_id_that_is_not_a_sevdesk_id_is_a_422(
    client: AsyncClient, session: AsyncSession, contact_id: object
):
    party = await _parent(session)

    response = await client.put(f"/parties/{party}/sevdesk-contact", json={"sevdesk_contact_id": contact_id})

    assert response.status_code == 422
    assert await _contacts(session) == []


# --- PUT and DELETE /parties/{party_id}/manual-billing ---


async def _marks(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(ManualPayer)) or 0


async def test_put_marks_a_payer_billed_by_hand_and_the_same_note_changes_nothing(
    client: AsyncClient, session: AsyncSession
):
    party = await _parent(session)
    url = f"/parties/{party}/manual-billing"

    first = await client.put(url, json={"note": "authority, by hand"})
    version = await _row_version(session, "billing.manual_payer", party)
    again = await client.put(url, json={"note": "authority, by hand"})

    assert first.status_code == again.status_code == 200
    assert first.json() == {
        "party_id": str(party),
        "note": "authority, by hand",
        "created_at": ANY,
        "updated_at": ANY,
    }
    assert again.json() == first.json()
    assert await _row_version(session, "billing.manual_payer", party) == version
    assert await _marks(session) == 1


async def test_put_replaces_the_note_and_leaving_it_out_clears_it(client: AsyncClient, session: AsyncSession):
    party = await _parent(session)
    url = f"/parties/{party}/manual-billing"
    await client.put(url, json={"note": "first"})
    version = await _row_version(session, "billing.manual_payer", party)

    assert (await client.put(url, json={"note": "second"})).json()["note"] == "second"
    assert await _row_version(session, "billing.manual_payer", party) != version
    assert (await client.put(url, json={})).json()["note"] is None
    assert await _marks(session) == 1


async def test_delete_unmarks_and_an_unmarked_party_answers_204_too(client: AsyncClient, session: AsyncSession):
    party = await _parent(session)
    url = f"/parties/{party}/manual-billing"
    await client.put(url, json={})

    assert (await client.delete(url)).status_code == 204
    assert await _marks(session) == 0
    assert (await client.delete(url)).status_code == 204
