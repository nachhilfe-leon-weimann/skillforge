"""Pins status, ``code`` and ``detail`` of every billing error, per route (billing spec, "Error catalog").

Each case stubs the service function an endpoint calls, makes it raise the domain error with an *internal* instance
message, and asserts what a client sees. Every billing slice adds its own rows; the catalog stays closed.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.api.v1.common import status_for
from app.core.auth import AuthSettings, Scope, create_application_access_token
from app.core.auth.dependencies import get_auth_settings
from app.core.db.dependencies import get_db_session
from app.core.errors import DomainError
from app.main import app
from app.services.billing.errors import (
    CalendarAliasAlreadyExistsError,
    CalendarAliasNotFoundError,
    CalendarEventAlreadyAssignedError,
    CalendarEventNotAssignableError,
    CalendarEventNotFoundError,
    CalendarUnavailableError,
    LessonAlreadyExistsError,
    LessonNotBillableError,
    LessonNotFoundError,
    RateNotFoundError,
    RateNotQuarterHourError,
    SevdeskContactAlreadyLinkedError,
    UnknownStudentError,
)
from app.services.crm.errors import PartyNotFoundError

ID = "00000000-0000-0000-0000-0000000000aa"
INTERNAL = "internal: row 7f3a"
LESSON = {"student_id": ID, "subject": "Mathe", "starts_at": "2026-11-02T16:00:00+01:00", "minutes": 60}
ALREADY_EXISTS = "The student already has a lesson at this start"
NOT_QUARTER_HOUR = "A rate must be at least 0 cents per hour and divisible by 4"
NOT_BILLABLE = "Only a held or cancelled lesson can be billable"
ALREADY_LINKED = "The party or the sevDesk contact is already linked"
ALIAS_EXISTS = "An alias for this calendar name already exists"
EVENT_ASSIGNED = "The calendar event already has a lesson"
EVENT_NOT_ASSIGNABLE = "The calendar event cannot become a lesson: its start is not on a whole minute"
CALENDAR_UNAVAILABLE = "The lesson calendar could not be read"
ASSIGNMENT = {"student_id": ID, "subject": "Mathe", "minutes": 60}


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    stub: str
    """The service function the endpoint calls, as ``<module>.<name>`` under ``app.services.billing``."""
    json: dict[str, Any] | None = None


ENDPOINTS = {
    "create_lesson": Endpoint("POST", "/lessons", "lessons.create_lesson", json=LESSON),
    "get_lesson": Endpoint("GET", f"/lessons/{ID}", "lessons.get_lesson"),
    "update_lesson": Endpoint("PATCH", f"/lessons/{ID}", "lessons.update_lesson", json={"status": "held"}),
    "list_rates": Endpoint("GET", f"/students/{ID}/rates", "rates.list_rates"),
    "put_rate": Endpoint("PUT", f"/students/{ID}/rates/2026-09-01", "rates.put_rate", json={"cents_per_hour": 3000}),
    "delete_rate": Endpoint("DELETE", f"/students/{ID}/rates/2026-09-01", "rates.delete_rate"),
    "get_student_payer": Endpoint("GET", f"/students/{ID}/payer", "payers.get_student_payer"),
    "resolve_lesson_payer": Endpoint("POST", f"/lessons/{ID}/resolve-payer", "payers.resolve_lesson_payer"),
    "put_sevdesk_contact": Endpoint(
        "PUT", f"/parties/{ID}/sevdesk-contact", "payers.link_sevdesk_contact", json={"sevdesk_contact_id": "1234567"}
    ),
    "delete_sevdesk_contact": Endpoint("DELETE", f"/parties/{ID}/sevdesk-contact", "payers.unlink_sevdesk_contact"),
    "put_manual_billing": Endpoint("PUT", f"/parties/{ID}/manual-billing", "payers.put_manual_payer", json={}),
    "delete_manual_billing": Endpoint("DELETE", f"/parties/{ID}/manual-billing", "payers.delete_manual_payer"),
    "create_calendar_alias": Endpoint(
        "POST", "/calendar/aliases", "calendar_aliases.create_alias", json={"name": "Student B", "student_id": ID}
    ),
    "delete_calendar_alias": Endpoint("DELETE", f"/calendar/aliases/{ID}", "calendar_aliases.delete_alias"),
    "import_calendar": Endpoint(
        "POST",
        "/calendar/import",
        "calendar.import_calendar",
        json={"first_day": "2026-11-02", "last_day": "2026-11-08"},
    ),
    "assign_calendar_event": Endpoint(
        "POST", f"/calendar/events/{ID}/assign", "calendar.assign_event", json=ASSIGNMENT
    ),
    "dismiss_calendar_event": Endpoint("POST", f"/calendar/events/{ID}/dismiss", "calendar.dismiss_event"),
}

# (endpoint, raised error, status, detail) - one row per error a route documents.
type Expectation = tuple[str, DomainError, int, str]

EXPECTATIONS: list[Expectation] = [
    ("create_lesson", UnknownStudentError(INTERNAL), 422, "Unknown student"),
    ("create_lesson", RateNotQuarterHourError(INTERNAL), 422, NOT_QUARTER_HOUR),
    ("create_lesson", LessonAlreadyExistsError(INTERNAL), 409, ALREADY_EXISTS),
    ("get_lesson", LessonNotFoundError(INTERNAL), 404, "Lesson not found"),
    ("update_lesson", LessonNotFoundError(INTERNAL), 404, "Lesson not found"),
    ("update_lesson", UnknownStudentError(INTERNAL), 422, "Unknown student"),
    ("update_lesson", RateNotQuarterHourError(INTERNAL), 422, NOT_QUARTER_HOUR),
    ("update_lesson", LessonNotBillableError(INTERNAL), 422, NOT_BILLABLE),
    ("update_lesson", LessonAlreadyExistsError(INTERNAL), 409, ALREADY_EXISTS),
    ("put_rate", UnknownStudentError(INTERNAL), 422, "Unknown student"),
    ("put_rate", RateNotQuarterHourError(INTERNAL), 422, NOT_QUARTER_HOUR),
    ("delete_rate", RateNotFoundError(INTERNAL), 404, "Rate not found"),
    ("resolve_lesson_payer", LessonNotFoundError(INTERNAL), 404, "Lesson not found"),
    ("put_sevdesk_contact", SevdeskContactAlreadyLinkedError(INTERNAL), 409, ALREADY_LINKED),
    ("create_calendar_alias", UnknownStudentError(INTERNAL), 422, "Unknown student"),
    ("create_calendar_alias", CalendarAliasAlreadyExistsError(INTERNAL), 409, ALIAS_EXISTS),
    ("delete_calendar_alias", CalendarAliasNotFoundError(INTERNAL), 404, "Calendar alias not found"),
    ("import_calendar", CalendarUnavailableError(INTERNAL), 502, CALENDAR_UNAVAILABLE),
    ("assign_calendar_event", CalendarEventNotFoundError(INTERNAL), 404, "Calendar event not found"),
    ("assign_calendar_event", UnknownStudentError(INTERNAL), 422, "Unknown student"),
    ("assign_calendar_event", CalendarEventNotAssignableError(INTERNAL), 422, EVENT_NOT_ASSIGNABLE),
    ("assign_calendar_event", CalendarEventAlreadyAssignedError(INTERNAL), 409, EVENT_ASSIGNED),
    ("assign_calendar_event", LessonAlreadyExistsError(INTERNAL), 409, ALREADY_EXISTS),
    ("assign_calendar_event", CalendarAliasAlreadyExistsError(INTERNAL), 409, ALIAS_EXISTS),
    ("dismiss_calendar_event", CalendarEventNotFoundError(INTERNAL), 404, "Calendar event not found"),
]

# The CRM's errors a billing route answers with (billing spec, "Route map": a party in a path that does not exist),
# pinned here too; their classes are the CRM's, pinned by its own error contract. (endpoint, error, status, detail)
CRM_EXPECTATIONS: list[Expectation] = [
    ("list_rates", PartyNotFoundError(INTERNAL), 404, "Party not found"),
    ("put_rate", PartyNotFoundError(INTERNAL), 404, "Party not found"),
    ("delete_rate", PartyNotFoundError(INTERNAL), 404, "Party not found"),
    ("get_student_payer", PartyNotFoundError(INTERNAL), 404, "Party not found"),
    ("put_sevdesk_contact", PartyNotFoundError(INTERNAL), 404, "Party not found"),
    ("delete_sevdesk_contact", PartyNotFoundError(INTERNAL), 404, "Party not found"),
    ("put_manual_billing", PartyNotFoundError(INTERNAL), 404, "Party not found"),
    ("delete_manual_billing", PartyNotFoundError(INTERNAL), 404, "Party not found"),
]
CRM_CODES: dict[type[DomainError], str] = {PartyNotFoundError: "party_not_found"}

# class -> (code, status, public message, expose_message): the classes of the catalog that exist so far.
CATALOG: dict[type[DomainError], tuple[str, int, str, bool]] = {
    LessonNotFoundError: ("lesson_not_found", 404, "Lesson not found", False),
    RateNotFoundError: ("rate_not_found", 404, "Rate not found", False),
    CalendarAliasNotFoundError: ("calendar_alias_not_found", 404, "Calendar alias not found", False),
    CalendarEventNotFoundError: ("calendar_event_not_found", 404, "Calendar event not found", False),
    LessonAlreadyExistsError: ("lesson_already_exists", 409, ALREADY_EXISTS, False),
    SevdeskContactAlreadyLinkedError: ("sevdesk_contact_already_linked", 409, ALREADY_LINKED, False),
    CalendarAliasAlreadyExistsError: ("calendar_alias_already_exists", 409, ALIAS_EXISTS, False),
    CalendarEventAlreadyAssignedError: ("calendar_event_already_assigned", 409, EVENT_ASSIGNED, False),
    UnknownStudentError: ("unknown_student", 422, "Unknown student", False),
    RateNotQuarterHourError: ("rate_not_quarter_hour", 422, NOT_QUARTER_HOUR, False),
    LessonNotBillableError: ("lesson_not_billable", 422, NOT_BILLABLE, False),
    CalendarEventNotAssignableError: ("calendar_event_not_assignable", 422, EVENT_NOT_ASSIGNABLE, False),
    CalendarUnavailableError: ("calendar_unavailable", 502, CALENDAR_UNAVAILABLE, False),
}

# The closed catalog of the billing spec: no class outside it may ever exist.
SPEC_CATALOG = {
    "LessonNotFoundError", "RateNotFoundError", "CalendarEventNotFoundError", "CalendarAliasNotFoundError",
    "InvoiceNotFoundError", "PrepaidAccountNotFoundError", "PrepaidEntryNotFoundError",
    "LessonAlreadyExistsError", "LessonChargedError", "CalendarAliasAlreadyExistsError",
    "CalendarEventAlreadyAssignedError", "SevdeskContactAlreadyLinkedError", "PreviewOutdatedError",
    "InvoiceStateError", "PrepaidAccountAlreadyOpenError", "PrepaidAccountNotSettledError",
    "PrepaidAccountStartsBeforeLastCloseError", "PrepaidEntryAlreadyVoidedError",
    "UnknownStudentError", "RateNotQuarterHourError", "LessonNotBillableError", "CalendarEventNotAssignableError",
    "MixedPayersError", "InvoiceBlockedError", "PrepaidEntryNotVoidableError", "TopupAmountOutOfRangeError",
    "SevdeskUnavailableError", "SevdeskRefusedError", "CalendarUnavailableError",
}  # fmt: skip
BILLING_PACKAGES = ("app.services.billing", "app.api.v1.billing")
DERIVED_CODES = {"validation_error", "missing_token", "invalid_token", "forbidden"}


def _expectation_id(expectation: Expectation) -> str:
    name, error, _, _ = expectation
    return f"{name}-{type(error).__name__}"


@pytest.mark.parametrize("expectation", EXPECTATIONS, ids=_expectation_id)
async def test_billing_domain_error_status_code_and_detail(expectation: Expectation, monkeypatch):
    name, error, status, detail = expectation
    endpoint = ENDPOINTS[name]
    monkeypatch.setattr(f"app.services.billing.{endpoint.stub}", _raises(error))

    async with _client() as client:
        response = await client.request(
            endpoint.method, f"/api/v1/billing{endpoint.path}", json=endpoint.json, headers=_auth_headers()
        )

    assert response.status_code == status
    assert response.json() == {"detail": detail, "code": CATALOG[type(error)][0]}
    assert INTERNAL not in response.text


@pytest.mark.parametrize("expectation", CRM_EXPECTATIONS, ids=_expectation_id)
async def test_crm_error_on_a_billing_route_status_code_and_detail(expectation: Expectation, monkeypatch):
    name, error, status, detail = expectation
    endpoint = ENDPOINTS[name]
    monkeypatch.setattr(f"app.services.billing.{endpoint.stub}", _raises(error))

    async with _client() as client:
        response = await client.request(
            endpoint.method, f"/api/v1/billing{endpoint.path}", json=endpoint.json, headers=_auth_headers()
        )

    assert response.status_code == status
    assert response.json() == {"detail": detail, "code": CRM_CODES[type(error)]}
    assert INTERNAL not in response.text


@pytest.mark.parametrize("error_type", CATALOG, ids=lambda error_type: error_type.__name__)
def test_catalog_class_carries_its_code_message_and_exposure(error_type: type[DomainError]):
    code, status, message, expose_message = CATALOG[error_type]

    assert (error_type.code, status_for(error_type), error_type.message, error_type.expose_message) == (
        code,
        status,
        message,
        expose_message,
    )


def _subclasses(error_type: type[DomainError]) -> set[type[DomainError]]:
    found = set(error_type.__subclasses__())
    return found.union(*(_subclasses(subclass) for subclass in found))


def test_every_billing_error_is_pinned_and_in_the_closed_catalog():
    from app.services.billing import errors

    # Importing the app has imported every billing module, so a class defined outside errors.py shows up.
    defined = {error for error in _subclasses(DomainError) if error.__module__.startswith(BILLING_PACKAGES)}

    assert defined == set(CATALOG)
    assert {error.__name__ for error in defined} <= SPEC_CATALOG
    assert {error.__module__ for error in defined} == {errors.__name__}
    assert {type(error) for _, error, _, _ in EXPECTATIONS} == set(CATALOG)


def test_every_pinned_error_is_documented_on_its_route():
    for name, error, status, _ in EXPECTATIONS + CRM_EXPECTATIONS:
        responses = _operation(ENDPOINTS[name])["responses"]
        examples = responses[str(status)]["content"]["application/json"]["examples"]
        assert type(error).code in examples, f"{name}: {type(error).__name__}"


def _documented_codes(operation: dict[str, Any]) -> set[str]:
    """Every error example of an operation, minus the derived ones."""
    return {
        code
        for response in operation["responses"].values()
        for code in response.get("content", {}).get("application/json", {}).get("examples", {})
    } - DERIVED_CODES


def test_no_route_documents_a_domain_error_that_is_not_pinned():
    pinned = {(name, type(error).code) for name, error, _, _ in EXPECTATIONS + CRM_EXPECTATIONS}

    for name, endpoint in ENDPOINTS.items():
        for code in _documented_codes(_operation(endpoint)):
            assert (name, code) in pinned, f"{name} documents {code} without a pinned expectation"


def test_every_billing_operation_that_documents_an_error_is_in_the_table():
    covered = {_operation(endpoint)["operationId"] for endpoint in ENDPOINTS.values()}
    documenting = [
        operation["operationId"]
        for path, item in app.openapi()["paths"].items()
        if path.startswith("/api/v1/billing/")
        for operation in item.values()
        if _documented_codes(operation)
    ]

    assert sorted(documenting) == sorted(covered)


def _operation(endpoint: Endpoint) -> dict[str, Any]:
    """Find the OpenAPI operation whose path template matches the endpoint's concrete path."""
    concrete = f"/api/v1/billing{endpoint.path}"
    method = endpoint.method.lower()
    matches = [
        item[method]
        for template, item in app.openapi()["paths"].items()
        if method in item and re.fullmatch(re.sub(r"\{[^/]+\}", "[^/]+", template), concrete)
    ]
    assert len(matches) == 1, concrete
    return matches[0]


def _raises(error: Exception):
    async def _inner(*args, **kwargs):
        raise error

    return _inner


async def _override_db_session() -> AsyncIterator[object]:
    yield object()


@asynccontextmanager
async def _client() -> AsyncGenerator[AsyncClient]:
    app.dependency_overrides[get_db_session] = _override_db_session
    app.dependency_overrides[get_auth_settings] = lambda: _auth_settings()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def _auth_headers() -> dict[str, str]:
    token = create_application_access_token(
        _auth_settings(),
        principal_id=UUID("00000000-0000-0000-0000-000000000001"),
        client_id="operator",
        scopes=[str(Scope.BILLING_READ), str(Scope.BILLING_WRITE)],
    )
    return {"Authorization": f"Bearer {token.access_token}"}


def _auth_settings() -> AuthSettings:
    return AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
