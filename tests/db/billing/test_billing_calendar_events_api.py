"""The calendar routes (billing spec, SF4-3), on Postgres, with Graph behind a mocked transport.

`POST /calendar/import` answers with counts only and maps every failure of the calendar to `502
calendar_unavailable`, writing nothing; `GET /calendar/events` lists what waits or changed; `assign` and `dismiss`
act on one occurrence. Every name is synthetic.
"""

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.billing.params import get_calendar_reader
from app.core.db.models import CalendarAlias, CalendarEvent, Lesson
from app.core.logging import LogFormat, LoggingSettings, LogLevel, configure_logging
from app.integrations.graph.config import GraphSettings
from app.main import app
from app.services.crm import companies
from tests.db.billing.calendar_support import (
    MAX,
    answer_view,
    fixture_events,
    graph_event,
    graph_settings,
    new_student,
    reader,
    snapshot,
)
from tests.integrations.graph.fake_graph import (
    CALENDARS,
    TOKEN_REQUEST,
    VIEW_PATH,
    Credential,
    FakeGraph,
    answer,
)

pytestmark = pytest.mark.db

IMPORT = "/calendar/import"
EVENTS = "/calendar/events"
G2_RANGE = {"first_day": "2026-11-23", "last_day": "2026-12-13"}
DAY = dt.date(2026, 12, 2)
ONE_DAY = {"first_day": DAY.isoformat(), "last_day": DAY.isoformat()}
START = dt.datetime(2026, 12, 2, 15, tzinfo=dt.UTC)
UNAVAILABLE = {"detail": "The lesson calendar could not be read", "code": "calendar_unavailable"}
COUNTS = {"occurrences", "lessons_created", "lessons_updated", "lessons_removed", "changed", "unassigned", "deleted"}


@pytest.fixture(scope="module")
def credential() -> Credential:
    return Credential.generate()


@pytest.fixture
def fake() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
def settings(credential: Credential) -> GraphSettings:
    return graph_settings(credential)


@pytest.fixture
async def api(client: AsyncClient, fake: FakeGraph, settings: GraphSettings) -> AsyncIterator[AsyncClient]:
    """The billing client, its import reading the fake; ``client`` clears the override afterwards."""
    app.dependency_overrides[get_calendar_reader] = lambda: reader(fake, settings)
    yield client


@pytest.fixture
def restore_logging():
    yield
    configure_logging(LoggingSettings())


async def _import(api: AsyncClient, days: dict[str, str]) -> dict[str, Any]:
    response = await api.post(IMPORT, json=days)
    assert response.status_code == 200, response.text
    return response.json()


async def _event_id(session: AsyncSession, external_id: str) -> uuid.UUID:
    return (await session.scalars(select(CalendarEvent.id).where(CalendarEvent.external_id == external_id))).one()


async def _waiting(api: AsyncClient, fake: FakeGraph, session: AsyncSession) -> uuid.UUID:
    """One occurrence of `DAY` that waits for an assignment: its name matches nobody."""
    answer_view(fake, [graph_event("waiting", "Mathe mit Student Z", START)])
    await _import(api, ONE_DAY)
    return await _event_id(session, "waiting")


# --- import ---


async def test_the_import_answers_with_counts_only_and_logs_no_name(
    api: AsyncClient, fake: FakeGraph, session: AsyncSession, restore_logging, capsys, caplog
):
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()
    await new_student(session, MAX)
    answer_view(fake, [*fixture_events("g2-series-first-read"), graph_event("waiting", "Mathe mit Student Z", START)])

    response = await api.post(IMPORT, json=G2_RANGE)
    fake.on("GET", VIEW_PATH, answer("g6-mailbox-outside-scope"))
    failed = await api.post(IMPORT, json=G2_RANGE)

    assert response.status_code == 200
    assert response.json() == {
        "occurrences": 4,
        "lessons_created": 3,
        "lessons_updated": 0,
        "lessons_removed": 0,
        "changed": 0,
        "unassigned": 1,
        "deleted": 0,
    }
    assert (failed.status_code, failed.json()) == (502, UNAVAILABLE)
    output = capsys.readouterr().out + caplog.text
    assert '"calendar_imported"' in output and '"calendar_unavailable"' in output
    for text in (response.text, failed.text, output):
        for name in ("Max", "Mustermann", "Student Z", "Mathematik", "Mathe mit"):
            assert name not in text


@pytest.mark.parametrize(
    "failure",
    ["refused", "identity", "unavailable", "transport", "unexpected", "token", "settings"],
)
async def test_every_failure_of_the_calendar_is_502_calendar_unavailable_and_writes_nothing(
    api: AsyncClient, fake: FakeGraph, session: AsyncSession, failure: str
):
    await new_student(session, MAX)
    answer_view(fake, fixture_events("g2-series-first-read"))
    await _import(api, G2_RANGE)
    answer_view(fake, fixture_events("g2-series-second-read"))
    before = await snapshot(session)
    match failure:
        case "refused":
            fake.on("GET", VIEW_PATH, answer("g6-mailbox-outside-scope"))
        case "identity":
            fake.on("GET", CALENDARS["request"]["path"], httpx.Response(200, json={"value": []}))
        case "unavailable":
            fake.on("GET", VIEW_PATH, httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}}))
        case "transport":
            fake.on("GET", VIEW_PATH, httpx.ConnectError("connection refused"))
        case "unexpected":
            fake.on("GET", VIEW_PATH, httpx.Response(200, content=b"<html>not json</html>"))
        case "token":
            fake.on("POST", TOKEN_REQUEST["path"], answer("g7-token-error"))
        case "settings":
            app.dependency_overrides[get_calendar_reader] = lambda: reader(fake, GraphSettings(_env_file=None))

    response = await api.post(IMPORT, json=G2_RANGE)

    assert (response.status_code, response.json()) == (502, UNAVAILABLE)
    assert await snapshot(session) == before


@pytest.mark.parametrize(
    "days",
    [
        {"first_day": "2026-12-02", "last_day": "2026-12-01"},
        {"first_day": "2026-01-01", "last_day": "2026-04-03"},
        {"first_day": "0001-01-01", "last_day": "0001-01-02"},
        {"first_day": "2026-12-01"},
    ],
    ids=["reversed", "longer-than-92-days", "first-day-of-the-calendar", "no-last-day"],
)
async def test_a_range_that_is_no_range_of_days_is_a_validation_error(
    api: AsyncClient, fake: FakeGraph, days: dict[str, str]
):
    response = await api.post(IMPORT, json=days)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert fake.requests == []


async def test_92_days_are_imported_in_one_range(api: AsyncClient, fake: FakeGraph):
    answer_view(fake, [])

    assert (await api.post(IMPORT, json={"first_day": "2026-01-01", "last_day": "2026-04-02"})).status_code == 200


# --- the list ---


async def test_the_list_holds_what_waits_and_what_changed_by_start(
    api: AsyncClient, fake: FakeGraph, session: AsyncSession
):
    await new_student(session, MAX)
    first_read = fixture_events("g2-series-first-read")
    waiting = graph_event("waiting", "Mathe mit Student Z", dt.datetime(2026, 11, 30, 15, tzinfo=dt.UTC))
    phone = graph_event("phone", "Mathe mit Student Y", START, categories=["Telefon"])
    answer_view(fake, [*first_read, waiting, phone])
    await _import(api, G2_RANGE)
    ticked = (await session.scalars(select(Lesson).order_by(Lesson.starts_at).offset(1))).first()
    assert ticked is not None
    patched = await api.patch(f"/lessons/{ticked.id}", json={"status": "held"})
    assert patched.status_code == 200
    answer_view(fake, [*fixture_events("g2-series-second-read"), waiting, phone])
    await _import(api, G2_RANGE)

    listed = (await api.get(EVENTS)).json()

    assert listed["total"] == 2
    first, second = listed["items"]
    assert first == {
        "id": str(await _event_id(session, "waiting")),
        "starts_at": "2026-11-30T15:00:00Z",
        "ends_at": "2026-11-30T16:00:00Z",
        "title_subject": "Mathe",
        "title_name": "Student Z",
        "cancelled": False,
        "reason": "unmatched",
        "changed": False,
        "vanished": False,
        "lesson_id": None,
        "last_seen_at": first["last_seen_at"],
    }
    assert (second["starts_at"], second["changed"], second["lesson_id"], second["title_name"]) == (
        "2026-12-03T14:00:00Z",
        True,
        str(ticked.id),
        None,
    )


# --- assign ---


async def test_assign_creates_the_lesson_through_the_lesson_services_and_clears_the_name(
    api: AsyncClient, fake: FakeGraph, session: AsyncSession
):
    student = await new_student(session, ("Moritz", "Beispiel"))
    event_id = await _waiting(api, fake, session)

    response = await api.post(
        f"{EVENTS}/{event_id}/assign",
        json={"student_id": str(student), "subject": " Mathe ", "minutes": 45, "alias": "Student Z"},
    )

    assert response.status_code == 201
    lesson = response.json()
    assert response.headers["location"] == f"/api/v1/billing/lessons/{lesson['id']}"
    assert (lesson["student_id"], lesson["subject"], lesson["minutes"], lesson["status"]) == (
        str(student),
        "Mathe",
        45,
        "planned",
    )
    assert lesson["starts_at"] == "2026-12-02T15:00:00Z"
    event = await session.get(CalendarEvent, event_id, populate_existing=True)
    assert event is not None and (event.title_name, event.reason) == (None, None)
    assert (await api.get(EVENTS)).json()["total"] == 0
    aliases = list(await session.scalars(select(CalendarAlias.name_normalized)))
    assert aliases == ["student z"]
    # The alias holds for the next import: the occurrence keeps its one lesson.
    assert (await _import(api, ONE_DAY))["lessons_created"] == 0


async def test_assign_of_a_cancelled_occurrence_creates_a_cancelled_lesson(
    api: AsyncClient, fake: FakeGraph, session: AsyncSession
):
    student = await new_student(session, ("Moritz", "Beispiel"))
    answer_view(fake, [graph_event("late", "Mathe mit Student Z", START, categories=["Abgesagt"])])
    await _import(api, ONE_DAY)

    response = await api.post(
        f"{EVENTS}/{await _event_id(session, 'late')}/assign",
        json={"student_id": str(student), "subject": "Mathe", "minutes": 60},
    )

    assert (response.status_code, response.json()["status"], response.json()["billable"]) == (201, "cancelled", False)


async def test_a_second_assign_is_calendar_event_already_assigned(
    api: AsyncClient, fake: FakeGraph, session: AsyncSession
):
    student = await new_student(session, ("Moritz", "Beispiel"))
    event_id = await _waiting(api, fake, session)
    body = {"student_id": str(student), "subject": "Mathe", "minutes": 60}
    assert (await api.post(f"{EVENTS}/{event_id}/assign", json=body)).status_code == 201

    response = await api.post(f"{EVENTS}/{event_id}/assign", json={**body, "minutes": 45})

    assert (response.status_code, response.json()) == (
        409,
        {"detail": "The calendar event already has a lesson", "code": "calendar_event_already_assigned"},
    )
    assert await session.scalar(select(func.count()).select_from(Lesson)) == 1


async def test_assign_of_an_occurrence_the_import_matched_is_calendar_event_already_assigned(
    api: AsyncClient, fake: FakeGraph, session: AsyncSession
):
    student = await new_student(session, MAX)
    answer_view(fake, [graph_event("matched", "Mathe mit Max Mustermann", START)])
    await _import(api, ONE_DAY)

    response = await api.post(
        f"{EVENTS}/{await _event_id(session, 'matched')}/assign",
        json={"student_id": str(student), "subject": "Mathe", "minutes": 60},
    )

    assert response.json()["code"] == "calendar_event_already_assigned"


async def test_assign_of_an_unknown_occurrence_is_calendar_event_not_found(api: AsyncClient, student: uuid.UUID):
    response = await api.post(
        f"{EVENTS}/{uuid.uuid4()}/assign", json={"student_id": str(student), "subject": "Mathe", "minutes": 60}
    )

    assert (response.status_code, response.json()) == (
        404,
        {"detail": "Calendar event not found", "code": "calendar_event_not_found"},
    )


async def test_assign_to_a_party_that_is_no_student_is_unknown_student_and_writes_nothing(
    api: AsyncClient, fake: FakeGraph, session: AsyncSession
):
    company = await companies.create_company(session, name="Company A")
    event_id = await _waiting(api, fake, session)
    before = await snapshot(session)

    for party_id in (company.id, uuid.uuid4()):
        response = await api.post(
            f"{EVENTS}/{event_id}/assign",
            json={"student_id": str(party_id), "subject": "Mathe", "minutes": 60, "alias": "Student Z"},
        )
        assert (response.status_code, response.json()["code"]) == (422, "unknown_student")

    assert await snapshot(session) == before
    assert await session.scalar(select(func.count()).select_from(CalendarAlias)) == 0


async def test_assign_with_an_alias_that_exists_writes_nothing(
    api: AsyncClient, fake: FakeGraph, session: AsyncSession
):
    student = await new_student(session, ("Moritz", "Beispiel"))
    stored = await api.post("/calendar/aliases", json={"name": "Student Z", "student_id": str(student)})
    assert stored.status_code == 201
    other = await new_student(session, ("Lena", "Beispiel"))
    answer_view(fake, [graph_event("waiting", "Mathe mit Student Q", START)])
    await _import(api, ONE_DAY)
    before = await snapshot(session)

    response = await api.post(
        f"{EVENTS}/{await _event_id(session, 'waiting')}/assign",
        json={"student_id": str(other), "subject": "Mathe", "minutes": 60, "alias": "student-z"},
    )

    assert (response.status_code, response.json()["code"]) == (409, "calendar_alias_already_exists")
    assert await snapshot(session) == before


@pytest.mark.parametrize("minutes", [50, 0], ids=["off-the-quarter-hour", "none"])
async def test_assign_takes_minutes_on_the_quarter_hour_only(
    api: AsyncClient, fake: FakeGraph, session: AsyncSession, student: uuid.UUID, minutes: int
):
    event_id = await _waiting(api, fake, session)

    response = await api.post(
        f"{EVENTS}/{event_id}/assign", json={"student_id": str(student), "subject": "Mathe", "minutes": minutes}
    )

    assert (response.status_code, response.json()["code"]) == (422, "validation_error")


# --- dismiss ---


async def test_dismiss_takes_an_occurrence_off_the_list_and_is_idempotent(
    api: AsyncClient, fake: FakeGraph, session: AsyncSession
):
    event_id = await _waiting(api, fake, session)
    assert (await api.get(EVENTS)).json()["total"] == 1

    first = await api.post(f"{EVENTS}/{event_id}/dismiss")
    after_first = await snapshot(session)
    second = await api.post(f"{EVENTS}/{event_id}/dismiss")

    assert (first.status_code, second.status_code) == (204, 204)
    assert await snapshot(session) == after_first
    assert (await api.get(EVENTS)).json() == {"items": [], "total": 0, "limit": 50, "offset": 0}
    event = await session.get(CalendarEvent, event_id, populate_existing=True)
    assert event is not None and (event.dismissed, event.title_name) == (True, None)


async def test_dismiss_of_an_unknown_occurrence_is_calendar_event_not_found(api: AsyncClient):
    response = await api.post(f"{EVENTS}/{uuid.uuid4()}/dismiss")

    assert (response.status_code, response.json()) == (
        404,
        {"detail": "Calendar event not found", "code": "calendar_event_not_found"},
    )


def test_the_import_response_names_no_field_but_counts():
    schema = app.openapi()["components"]["schemas"]["CalendarImportResponse"]

    assert set(schema["properties"]) == COUNTS
    assert {field["type"] for field in schema["properties"].values()} == {"integer"}
