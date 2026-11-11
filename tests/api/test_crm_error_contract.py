"""Pins status, ``code`` and ``detail`` of every row of the CRM error catalog, per route.

Each case stubs the service function an endpoint calls, makes it raise the domain error with an
*internal* instance message, and asserts what a client sees. Every slice of the CRM API spec adds
its own rows.
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

from app.core.auth import AuthSettings, Scope, create_application_access_token
from app.core.auth.dependencies import get_auth_settings
from app.core.db.dependencies import get_db_session
from app.core.errors import DomainError
from app.main import app
from app.services.crm.errors import (
    CompanyNotFoundError,
    ContactInfoAlreadyExistsError,
    ContactInfoNotFoundError,
    InvalidContactValueError,
    InvalidPartyRelationError,
    PartyInUseError,
    PartyNotFoundError,
    PartyRelationNotFoundError,
    PersonNotFoundError,
    RelatedPartyNotFoundError,
    RoleNotFoundError,
    SubjectAlreadyExistsError,
    SubjectInUseError,
    SubjectNotFoundError,
    UnknownSubjectError,
)

ID = "00000000-0000-0000-0000-0000000000aa"
OTHER_ID = "00000000-0000-0000-0000-0000000000bb"
INTERNAL = "internal: row 7f3a"
INVALID_RELATION = "Invalid party relation"
PARTY_IN_USE = "Party is still in use"
PERSON = {"firstname": "Max", "lastname": "Mustermann"}
EMAIL = {"type": "email", "value": "max.mustermann@example.com"}
CONTACT_INFO_EXISTS = "Contact info already exists for this party"
INVALID_CONTACT_VALUE = "Value is not valid for this contact info type"
STUDENT_ROLE = {"preferred_meeting_tool": "discord", "subject_ids": [1]}


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    stub: str
    """The service function the endpoint calls, as ``<module>.<name>`` under ``app.services.crm``."""
    json: dict[str, Any] | None = None


ENDPOINTS = {
    "get_party": Endpoint("GET", f"/parties/{ID}", "parties.load_party"),
    "delete_party": Endpoint("DELETE", f"/parties/{ID}", "parties.delete_party"),
    "create_person": Endpoint("POST", "/persons", "persons.create_person", json=PERSON),
    "put_student_role": Endpoint("PUT", f"/persons/{ID}/student", "roles.put_student_role", json=STUDENT_ROLE),
    "remove_student_role": Endpoint("DELETE", f"/persons/{ID}/student", "roles.remove_student_role"),
    "put_tutor_role": Endpoint("PUT", f"/persons/{ID}/tutor", "roles.put_tutor_role", json={"subject_ids": [1]}),
    "remove_tutor_role": Endpoint("DELETE", f"/persons/{ID}/tutor", "roles.remove_tutor_role"),
    "add_contact_info": Endpoint("POST", f"/parties/{ID}/contact-infos", "contact_infos.add_contact_info", json=EMAIL),
    "update_contact_info": Endpoint(
        "PATCH", f"/parties/{ID}/contact-infos/{ID}", "contact_infos.update_contact_info", json={"value": "a@b.example"}
    ),
    "remove_contact_info": Endpoint("DELETE", f"/parties/{ID}/contact-infos/{ID}", "contact_infos.remove_contact_info"),
    "list_relations": Endpoint("GET", f"/parties/{ID}/relations", "relations.list_relations"),
    "put_relation": Endpoint("PUT", f"/parties/{ID}/relations/parent_of/{OTHER_ID}", "relations.put_relation"),
    "remove_relation": Endpoint("DELETE", f"/parties/{ID}/relations/parent_of/{OTHER_ID}", "relations.remove_relation"),
    "update_person": Endpoint("PATCH", f"/persons/{ID}", "persons.update_person", json={"firstname": "Max"}),
    "update_company": Endpoint("PATCH", f"/companies/{ID}", "companies.update_company", json={"name": "Musterfirma"}),
    "create_subject": Endpoint("POST", "/subjects", "subjects.create_subject", json={"title": "Mathematics"}),
    "update_subject": Endpoint("PATCH", "/subjects/1", "subjects.update_subject", json={"title": "Mathematics"}),
    "delete_subject": Endpoint("DELETE", "/subjects/1", "subjects.delete_subject"),
}

# (endpoint, raised error, status, detail) - one row per cell of the spec's per-route table.
type Expectation = tuple[str, DomainError, int, str]

EXPECTATIONS: list[Expectation] = [
    ("get_party", PartyNotFoundError(INTERNAL), 404, "Party not found"),
    ("delete_party", PartyNotFoundError(INTERNAL), 404, "Party not found"),
    # expose_message: the service writes the detail for the client and names the kinds of links.
    (
        "delete_party",
        PartyInUseError(f"{PARTY_IN_USE}: discord_account, sevdesk_contact"),
        409,
        f"{PARTY_IN_USE}: discord_account, sevdesk_contact",
    ),
    ("delete_party", PartyInUseError(), 409, PARTY_IN_USE),
    ("create_person", UnknownSubjectError("Unknown subject: 5, 7"), 422, "Unknown subject: 5, 7"),
    ("create_person", UnknownSubjectError(), 422, "Unknown subject"),
    ("put_student_role", PersonNotFoundError(INTERNAL), 404, "Person not found"),
    ("put_student_role", UnknownSubjectError("Unknown subject: 5, 7"), 422, "Unknown subject: 5, 7"),
    ("remove_student_role", PersonNotFoundError(INTERNAL), 404, "Person not found"),
    ("remove_student_role", RoleNotFoundError(INTERNAL), 404, "Role not assigned"),
    ("put_tutor_role", PersonNotFoundError(INTERNAL), 404, "Person not found"),
    ("put_tutor_role", UnknownSubjectError("Unknown subject: 5, 7"), 422, "Unknown subject: 5, 7"),
    ("remove_tutor_role", PersonNotFoundError(INTERNAL), 404, "Person not found"),
    ("remove_tutor_role", RoleNotFoundError(INTERNAL), 404, "Role not assigned"),
    ("add_contact_info", PartyNotFoundError(INTERNAL), 404, "Party not found"),
    ("add_contact_info", ContactInfoAlreadyExistsError(INTERNAL), 409, CONTACT_INFO_EXISTS),
    ("update_contact_info", ContactInfoNotFoundError(INTERNAL), 404, "Contact info not found"),
    ("update_contact_info", ContactInfoAlreadyExistsError(INTERNAL), 409, CONTACT_INFO_EXISTS),
    ("update_contact_info", InvalidContactValueError(INTERNAL), 422, INVALID_CONTACT_VALUE),
    ("remove_contact_info", ContactInfoNotFoundError(INTERNAL), 404, "Contact info not found"),
    ("list_relations", PartyNotFoundError(INTERNAL), 404, "Party not found"),
    ("put_relation", PartyNotFoundError(INTERNAL), 404, "Party not found"),
    ("put_relation", RelatedPartyNotFoundError(INTERNAL), 404, "Related party not found"),
    (
        "put_relation",
        InvalidPartyRelationError(f"{INVALID_RELATION}: pays_for must point to a person"),
        422,
        f"{INVALID_RELATION}: pays_for must point to a person",
    ),
    ("put_relation", InvalidPartyRelationError(), 422, INVALID_RELATION),
    ("remove_relation", PartyRelationNotFoundError(INTERNAL), 404, "Party relation not found"),
    ("update_person", PersonNotFoundError(INTERNAL), 404, "Person not found"),
    ("update_company", CompanyNotFoundError(INTERNAL), 404, "Company not found"),
    ("create_subject", SubjectAlreadyExistsError(INTERNAL), 409, "Subject already exists"),
    ("update_subject", SubjectNotFoundError(INTERNAL), 404, "Subject not found"),
    ("update_subject", SubjectAlreadyExistsError(INTERNAL), 409, "Subject already exists"),
    ("delete_subject", SubjectNotFoundError(INTERNAL), 404, "Subject not found"),
    ("delete_subject", SubjectInUseError(INTERNAL), 409, "Subject is still assigned to students or tutors"),
]

# class -> (code, status, public message, expose_message): the catalog itself, independent of any route.
CATALOG: dict[type[DomainError], tuple[str, int, str, bool]] = {
    PartyNotFoundError: ("party_not_found", 404, "Party not found", False),
    PersonNotFoundError: ("person_not_found", 404, "Person not found", False),
    CompanyNotFoundError: ("company_not_found", 404, "Company not found", False),
    PartyInUseError: ("party_in_use", 409, PARTY_IN_USE, True),
    ContactInfoNotFoundError: ("contact_info_not_found", 404, "Contact info not found", False),
    ContactInfoAlreadyExistsError: ("contact_info_already_exists", 409, CONTACT_INFO_EXISTS, False),
    InvalidContactValueError: ("invalid_contact_value", 422, INVALID_CONTACT_VALUE, False),
    PartyRelationNotFoundError: ("party_relation_not_found", 404, "Party relation not found", False),
    RelatedPartyNotFoundError: ("related_party_not_found", 404, "Related party not found", False),
    InvalidPartyRelationError: ("invalid_party_relation", 422, INVALID_RELATION, True),
    RoleNotFoundError: ("role_not_found", 404, "Role not assigned", False),
    UnknownSubjectError: ("unknown_subject", 422, "Unknown subject", True),
    SubjectNotFoundError: ("subject_not_found", 404, "Subject not found", False),
    SubjectAlreadyExistsError: ("subject_already_exists", 409, "Subject already exists", False),
    SubjectInUseError: ("subject_in_use", 409, "Subject is still assigned to students or tutors", False),
}


def _expectation_id(expectation: Expectation) -> str:
    name, error, _, _ = expectation
    return f"{name}-{type(error).__name__}[{error}]"


@pytest.mark.parametrize("expectation", EXPECTATIONS, ids=_expectation_id)
async def test_crm_domain_error_status_code_and_detail(expectation: Expectation, monkeypatch):
    name, error, status, detail = expectation
    endpoint = ENDPOINTS[name]
    monkeypatch.setattr(f"app.services.crm.{endpoint.stub}", _raises(error))

    async with _client() as client:
        response = await client.request(
            endpoint.method, f"/api/v1/crm{endpoint.path}", json=endpoint.json, headers=_auth_headers()
        )

    assert response.status_code == status
    assert response.json() == {"detail": detail, "code": CATALOG[type(error)][0]}
    assert INTERNAL not in response.text


@pytest.mark.parametrize("error_type", CATALOG, ids=lambda error_type: error_type.__name__)
def test_catalog_class_carries_its_code_message_and_exposure(error_type: type[DomainError]):
    from app.api.v1.common import status_for

    code, status, message, expose_message = CATALOG[error_type]

    assert (error_type.code, status_for(error_type), error_type.message, error_type.expose_message) == (
        code,
        status,
        message,
        expose_message,
    )


def test_the_catalog_is_closed_at_the_fifteen_classes_of_the_spec():
    assert len(CATALOG) == 15
    assert len({code for code, *_ in CATALOG.values()}) == 15


# Codes the conventions derive for every route; a route never declares them.
DERIVED_CODES = {"validation_error", "missing_token", "invalid_token", "forbidden"}
CRM_PACKAGES = ("app.services.crm", "app.api.v1.crm")


def _subclasses(error_type: type[DomainError]) -> set[type[DomainError]]:
    found = set(error_type.__subclasses__())
    return found.union(*(_subclasses(subclass) for subclass in found))


def test_every_catalog_class_is_pinned():
    from app.services.crm import errors

    # Importing the app has imported every CRM module, so a class defined outside errors.py shows up.
    defined = {error for error in _subclasses(DomainError) if error.__module__.startswith(CRM_PACKAGES)}

    assert defined == set(CATALOG)
    assert {error.__module__ for error in defined} == {errors.__name__}
    assert {type(error) for _, error, _, _ in EXPECTATIONS} == set(CATALOG)


def test_every_pinned_error_is_documented_on_its_route():
    for name, error, status, _ in EXPECTATIONS:
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
    pinned = {(name, type(error).code) for name, error, _, _ in EXPECTATIONS}

    for name, endpoint in ENDPOINTS.items():
        for code in _documented_codes(_operation(endpoint)):
            assert (name, code) in pinned, f"{name} documents {code} without a pinned expectation"


def test_every_crm_operation_that_documents_an_error_is_in_the_table_and_uses_catalog_codes_only():
    catalog_codes = {code for code, *_ in CATALOG.values()}
    covered = {_operation(endpoint)["operationId"] for endpoint in ENDPOINTS.values()}
    documenting = 0

    for path, item in app.openapi()["paths"].items():
        if not path.startswith("/api/v1/crm/"):
            continue
        for operation in item.values():
            codes = _documented_codes(operation)
            assert codes <= catalog_codes, f"{operation['operationId']} documents {codes - catalog_codes}"
            if codes:
                documenting += 1
                assert operation["operationId"] in covered, operation["operationId"]

    assert documenting == len(ENDPOINTS)


def _operation(endpoint: Endpoint) -> dict[str, Any]:
    """Find the OpenAPI operation whose path template matches the endpoint's concrete path."""
    concrete = f"/api/v1/crm{endpoint.path}"
    method = endpoint.method.lower()
    paths = app.openapi()["paths"]
    if concrete in paths and method in paths[concrete]:
        return paths[concrete][method]

    matches = [
        item[method]
        for template, item in paths.items()
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
        scopes=[str(Scope.CRM_READ), str(Scope.CRM_WRITE)],
    )
    return {"Authorization": f"Bearer {token.access_token}"}


def _auth_settings() -> AuthSettings:
    return AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
