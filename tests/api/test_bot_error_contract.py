"""Pins status, ``detail`` and ``code`` of every domain error the bot API returns.

Each case stubs the service function an endpoint calls, makes it raise the domain error with an
*internal* instance message, and asserts what a client sees.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
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
from app.services.bot import (
    CommandEnvConflictError,
    CommandEnvNotFoundError,
    CommandEnvValidationError,
    GroupMembershipNotFoundError,
    JobNotClaimedError,
    JobNotFoundError,
    OperationNotFoundError,
    OperationNotPendingError,
    PermissionGroupNotFoundError,
    PrincipalNotFoundError,
    StudentContextNotFoundError,
    TransitionConflictError,
    TransitionValidationError,
    TutorContextNotFoundError,
)

ID = "00000000-0000-0000-0000-0000000000aa"
INTERNAL = "internal: row 7f3a"

COMMAND_ENV = {"guild_id": 1, "channel_id": 2, "kind": "tutor_cmd"}
AUTHZ_CHECK = {"actor_discord_id": 1, "action_key": "tutor.activate", "target_party_id": ID}
TUTOR_PREPARE = {"guild_id": 1, "tutor_discord_id": 2}
TUTOR_COMMIT = {"category_channel_id": 3, "command_channel_id": 4}
STUDENT_PREPARE = {"guild_id": 1, "student_discord_id": 5, "tutor_discord_id": 2}


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    stub: str
    """The service function the endpoint calls, as ``<module>.<name>`` under ``app.api.v1.bot``."""
    json: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


ENDPOINTS = {
    "read_principal": Endpoint("GET", "/runtime/principals/1", "runtime.get_principal_view"),
    "read_tutor_context": Endpoint("GET", "/runtime/tutors/1/2", "runtime.get_tutor_context_view"),
    "read_student_context": Endpoint("GET", "/runtime/students/1/2", "runtime.get_student_context_view"),
    "resolve_command_env": Endpoint(
        "GET", "/runtime/command-envs/resolve", "runtime.resolve_command_env", params=COMMAND_ENV
    ),
    "upsert_command_env": Endpoint("PUT", "/command-envs", "command_envs.upsert_command_env", json=COMMAND_ENV),
    "delete_command_env": Endpoint("DELETE", "/command-envs/1/2/tutor_cmd", "command_envs.delete_command_env"),
    "add_user_to_group": Endpoint("PUT", "/users/1/groups/staff", "users.add_user_to_group"),
    "remove_user_from_group": Endpoint("DELETE", "/users/1/groups/staff", "users.remove_user_from_group"),
    "check_authorization": Endpoint("POST", "/authz/check", "authz.check_authorization", json=AUTHZ_CHECK),
    "read_job": Endpoint("GET", f"/jobs/{ID}", "jobs.get_job"),
    "complete_job": Endpoint("POST", f"/jobs/{ID}/complete", "jobs.complete_job"),
    "fail_job": Endpoint("POST", f"/jobs/{ID}/fail", "jobs.fail_job", json={}),
    "read_operation": Endpoint("GET", f"/operations/{ID}", "operations.get_operation"),
    "cancel_operation": Endpoint("POST", f"/operations/{ID}/cancel", "operations.cancel_operation"),
}

PREPARE_ENDPOINTS = {
    "prepare_tutor_activation": Endpoint(
        "POST", "/tutors/activations/prepare", "tutors.prepare_tutor_activation", json=TUTOR_PREPARE
    ),
    "prepare_tutor_deactivation": Endpoint(
        "POST", "/tutors/1/2/deactivate/prepare", "tutors.prepare_tutor_deactivation"
    ),
    "prepare_student_activation": Endpoint(
        "POST", "/students/activations/prepare", "students.prepare_student_activation", json=STUDENT_PREPARE
    ),
    "prepare_student_stash": Endpoint("POST", "/students/1/5/stash/prepare", "students.prepare_student_stash"),
    "prepare_student_pop": Endpoint("POST", "/students/1/5/pop/prepare", "students.prepare_student_pop"),
    "prepare_student_deactivation": Endpoint(
        "POST", "/students/1/5/deactivate/prepare", "students.prepare_student_deactivation"
    ),
}

COMMIT_ENDPOINTS = {
    "commit_tutor_activation": Endpoint(
        "POST", f"/tutors/activations/{ID}/commit", "tutors.commit_tutor_activation", json=TUTOR_COMMIT
    ),
    "commit_tutor_deactivation": Endpoint(
        "POST", f"/tutors/1/2/deactivate/{ID}/commit", "tutors.commit_tutor_deactivation"
    ),
    "commit_student_activation": Endpoint(
        "POST", f"/students/activations/{ID}/commit", "students.commit_student_activation", json={"channel_id": 6}
    ),
    "commit_student_stash": Endpoint("POST", f"/students/1/5/stash/{ID}/commit", "students.commit_student_stash"),
    "commit_student_pop": Endpoint("POST", f"/students/1/5/pop/{ID}/commit", "students.commit_student_pop"),
    "commit_student_deactivation": Endpoint(
        "POST", f"/students/1/5/deactivate/{ID}/commit", "students.commit_student_deactivation"
    ),
}

# (endpoint, raised error, status, detail)
type Expectation = tuple[str, DomainError, int, str]

OPERATION_NOT_PENDING = "Operation is not in a prepared state (already committed, failed, or expired)"

EXPECTATIONS: list[Expectation] = [
    ("read_principal", PrincipalNotFoundError(INTERNAL), 404, "Discord principal not found"),
    ("read_tutor_context", TutorContextNotFoundError(INTERNAL), 404, "Tutor context not found"),
    ("read_student_context", StudentContextNotFoundError(INTERNAL), 404, "Student context not found"),
    ("resolve_command_env", CommandEnvNotFoundError(INTERNAL), 404, "Command env channel not found"),
    (
        "upsert_command_env",
        CommandEnvValidationError(INTERNAL),
        422,
        "Command env references an unknown guild, channel, or owner",
    ),
    (
        "upsert_command_env",
        CommandEnvConflictError(INTERNAL),
        409,
        "Owner already owns a command env of this kind in the guild",
    ),
    ("delete_command_env", CommandEnvNotFoundError(INTERNAL), 404, "Command env channel not found"),
    ("add_user_to_group", PrincipalNotFoundError(INTERNAL), 404, "Discord principal not found"),
    ("add_user_to_group", PermissionGroupNotFoundError(INTERNAL), 404, "Permission group not found"),
    ("remove_user_from_group", GroupMembershipNotFoundError(INTERNAL), 404, "Group membership not found"),
    ("check_authorization", PrincipalNotFoundError(INTERNAL), 404, "Discord principal not found"),
    ("read_job", JobNotFoundError(INTERNAL), 404, "Job not found"),
    ("complete_job", JobNotFoundError(INTERNAL), 404, "Job not found"),
    ("complete_job", JobNotClaimedError(INTERNAL), 409, "Job is not in a claimed state"),
    ("fail_job", JobNotFoundError(INTERNAL), 404, "Job not found"),
    ("fail_job", JobNotClaimedError(INTERNAL), 409, "Job is not in a claimed state"),
    ("read_operation", OperationNotFoundError(INTERNAL), 404, "Operation not found"),
    ("cancel_operation", OperationNotFoundError(INTERNAL), 404, "Operation not found"),
    ("cancel_operation", OperationNotPendingError("Operation has expired"), 409, "Operation has expired"),
    ("cancel_operation", OperationNotPendingError(), 409, OPERATION_NOT_PENDING),
    # A student activation is validated against the CRM (ADR 0007); each reason reaches the client as it is.
    *(
        ("prepare_student_activation", TransitionValidationError(reason), 422, reason)
        for reason in (
            "Student is not linked to a party",
            "Tutor is not linked to a party",
            "Tutor is not assigned to this student",
        )
    ),
    # Only these two commits re-check the student workspace, which may have vanished since prepare.
    (
        "commit_student_stash",
        TransitionValidationError("Student workspace not found"),
        422,
        "Student workspace not found",
    ),
    (
        "commit_student_pop",
        TransitionValidationError("Student workspace not found"),
        422,
        "Student workspace not found",
    ),
]

# Transition messages are written for clients ("Tutor student capacity reached") and shown as-is.
PREPARE_EXPECTATIONS: list[tuple[DomainError, int, str]] = [
    (TransitionConflictError("Tutor student capacity reached"), 409, "Tutor student capacity reached"),
    (TransitionConflictError(), 409, "Transition conflict"),
    (TransitionValidationError("Guild not found"), 422, "Guild not found"),
    (TransitionValidationError(), 422, "Transition validation failed"),
]
COMMIT_EXPECTATIONS: list[tuple[DomainError, int, str]] = [
    (OperationNotFoundError(INTERNAL), 404, "Operation not found"),
    (OperationNotPendingError("Operation has expired"), 409, "Operation has expired"),
    (OperationNotPendingError(), 409, OPERATION_NOT_PENDING),
    (TransitionConflictError("Tutor workspace no longer exists"), 409, "Tutor workspace no longer exists"),
]

ALL_ENDPOINTS = ENDPOINTS | PREPARE_ENDPOINTS | COMMIT_ENDPOINTS
ALL_EXPECTATIONS: list[Expectation] = [
    *EXPECTATIONS,
    *((name, *expected) for name in PREPARE_ENDPOINTS for expected in PREPARE_EXPECTATIONS),
    *((name, *expected) for name in COMMIT_ENDPOINTS for expected in COMMIT_EXPECTATIONS),
]


def _expectation_id(expectation: Expectation) -> str:
    name, error, _, _ = expectation
    return f"{name}-{type(error).__name__}[{error}]"


@pytest.mark.parametrize("expectation", ALL_EXPECTATIONS, ids=_expectation_id)
async def test_bot_domain_error_status_and_detail(expectation: Expectation, monkeypatch):
    name, error, status, detail = expectation
    endpoint = ALL_ENDPOINTS[name]
    monkeypatch.setattr(f"app.api.v1.bot.{endpoint.stub}", _raises(error))

    async with _client() as client:
        response = await client.request(
            endpoint.method,
            f"/api/v1/bot{endpoint.path}",
            json=endpoint.json,
            params=endpoint.params,
            headers=_auth_headers(),
        )

    assert response.status_code == status
    # The code values themselves are pinned per class in ``tests/test_bot_errors.py``.
    assert response.json() == {"detail": detail, "code": type(error).code}
    assert INTERNAL not in response.text


def test_every_pinned_error_is_documented_on_its_route():
    for name, error, status, _ in ALL_EXPECTATIONS:
        responses = _operation(ALL_ENDPOINTS[name])["responses"]
        examples = responses[str(status)]["content"]["application/json"]["examples"]
        assert type(error).code in examples, f"{name}: {type(error).__name__}"


def _operation(endpoint: Endpoint) -> dict[str, Any]:
    """Find the OpenAPI operation whose path template matches the endpoint's concrete path."""
    concrete = f"/api/v1/bot{endpoint.path}"
    matches = [
        item[endpoint.method.lower()]
        for template, item in app.openapi()["paths"].items()
        if endpoint.method.lower() in item and re.fullmatch(re.sub(r"\{[^/]+\}", "[^/]+", template), concrete)
    ]
    exact = [item for template, item in app.openapi()["paths"].items() if template == concrete]
    assert exact or len(matches) == 1, concrete
    return exact[0][endpoint.method.lower()] if exact else matches[0]


def test_every_endpoint_of_the_table_is_exercised():
    assert {name for name, *_ in ALL_EXPECTATIONS} == set(ALL_ENDPOINTS)


def _raises(error: Exception):
    async def _inner(*args, **kwargs):
        raise error

    return _inner


async def _override_db_session() -> AsyncIterator[object]:
    yield object()


@asynccontextmanager
async def _client() -> AsyncIterator[AsyncClient]:
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
        client_id="skillbot",
        scopes=[str(Scope.BOT_READ), str(Scope.BOT_WRITE)],
    )
    return {"Authorization": f"Bearer {token.access_token}"}


def _auth_settings() -> AuthSettings:
    return AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
