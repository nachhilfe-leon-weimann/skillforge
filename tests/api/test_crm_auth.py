"""Runtime guards of the CRM routes: 401 without a token, 403 without the scope, 500 on a failed commit.

The 401/403 cases run over every CRM operation of the OpenAPI document, so a route added by a later
slice is covered without touching this file. Only the reach-aware reads admit a person restricted to
`crm:read:own`; every other route answers them 403 (safe by default, ADR 0008).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.api.v1.common import ErrorResponse
from app.core.auth import (
    AuthMethod,
    AuthSettings,
    Scope,
    UserPrincipal,
    create_access_token,
    create_application_access_token,
)
from app.core.auth.dependencies import get_auth_settings
from app.core.db.dependencies import get_db_session
from app.core.db.models import Subject
from app.main import app

HTTP_METHODS = {"get", "put", "post", "delete", "patch"}
PATH_VALUES = {
    "subject_id": "1",
    "party_id": "00000000-0000-0000-0000-0000000000aa",
    "to_party_id": "00000000-0000-0000-0000-0000000000bb",
    "contact_info_id": "00000000-0000-0000-0000-0000000000cc",
    "type": "parent_of",
}
# The reach-aware reads (P0-7 of the user-authentication spec) demand `crm:read:own`, which `crm:read` implies.
REACH_AWARE = {
    ("GET", "/api/v1/crm/parties"),
    ("GET", "/api/v1/crm/parties/{party_id}".format(**PATH_VALUES)),
}


def _crm_operations() -> list[tuple[str, str]]:
    return [
        (method.upper(), path.format(**PATH_VALUES))
        for path, item in app.openapi()["paths"].items()
        if path.startswith("/api/v1/crm/")
        for method in item
        if method in HTTP_METHODS
    ]


def test_the_operation_table_is_not_empty():
    assert ("GET", "/api/v1/crm/subjects") in _crm_operations()
    assert ("DELETE", "/api/v1/crm/subjects/1") in _crm_operations()


@pytest.mark.parametrize(("method", "path"), _crm_operations())
async def test_crm_route_answers_401_without_a_token(method: str, path: str):
    async with _client() as client:
        response = await client.request(method, path)

    assert response.status_code == 401
    assert ErrorResponse.model_validate(response.json()).code == "unauthorized"
    scope = "crm:read" if method == "GET" else "crm:write"
    if (method, path) in REACH_AWARE:
        scope = "crm:read:own"
    assert response.headers["www-authenticate"] == f'Bearer scope="{scope}"'


@pytest.mark.parametrize(("method", "path"), _crm_operations())
async def test_crm_route_answers_403_with_a_token_lacking_the_scope(method: str, path: str):
    other_crm_scope = Scope.CRM_WRITE if method == "GET" else Scope.CRM_READ

    async with _client() as client:
        response = await client.request(method, path, headers=_auth_headers(other_crm_scope, Scope.AUTH_USERS_MANAGE))

    assert response.status_code == 403
    assert response.json() == {"detail": "Not enough permissions", "code": "forbidden"}


def test_the_operation_table_holds_both_reach_aware_reads():
    assert set(_crm_operations()) > REACH_AWARE


@pytest.mark.parametrize(
    ("method", "path"), [operation for operation in _crm_operations() if operation not in REACH_AWARE]
)
async def test_a_person_restricted_to_own_is_403_on_every_other_crm_route(method: str, path: str):
    async with _client() as client:
        response = await client.request(method, path, headers=_person_headers(Scope.ACCOUNT_SELF, Scope.CRM_READ_OWN))

    assert response.status_code == 403
    assert response.json() == {"detail": "Not enough permissions", "code": "forbidden"}


@pytest.mark.parametrize(("method", "path"), sorted(REACH_AWARE))
async def test_an_application_token_with_only_crm_read_own_is_403_on_both_reads(method: str, path: str):
    """An application has no party and so no reach: the body of a missing scope."""
    async with _client() as client:
        response = await client.request(method, path, headers=_auth_headers(Scope.CRM_READ_OWN))

    assert response.status_code == 403
    assert response.json() == {"detail": "Not enough permissions", "code": "forbidden"}


async def test_a_failing_commit_behind_a_crm_write_is_the_500_envelope_not_a_2xx(monkeypatch):
    """Decision N: with ``scope="function"`` the session ends before the response is sent."""

    async def create_subject(session, *, title):
        return Subject(id=1, title=title)

    async def failing_session() -> AsyncIterator[object]:
        yield object()
        raise RuntimeError("commit failed: connection 7f3a is gone")

    monkeypatch.setattr("app.services.crm.subjects.create_subject", create_subject)

    async with _client(raise_app_exceptions=False) as client:
        app.dependency_overrides[get_db_session] = failing_session
        response = await client.post(
            "/api/v1/crm/subjects", json={"title": "Mathematics"}, headers=_auth_headers(Scope.CRM_WRITE)
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error", "code": "internal_error"}


async def _override_db_session() -> AsyncIterator[object]:
    yield object()


@asynccontextmanager
async def _client(*, raise_app_exceptions: bool = True) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_db_session] = _override_db_session
    app.dependency_overrides[get_auth_settings] = lambda: _auth_settings()
    transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def _auth_headers(*scopes: Scope) -> dict[str, str]:
    token = create_application_access_token(
        _auth_settings(),
        principal_id=UUID("00000000-0000-0000-0000-000000000001"),
        client_id="operator",
        scopes=[str(scope) for scope in scopes],
    )
    return {"Authorization": f"Bearer {token.access_token}"}


def _person_headers(*scopes: Scope) -> dict[str, str]:
    principal = UserPrincipal(
        principal_id=uuid4(),
        client_id="portal",
        scopes=frozenset(str(scope) for scope in scopes),
        party_id=UUID(PATH_VALUES["party_id"]),
        session_id=uuid4(),
        roles=frozenset(),
        auth_methods=frozenset({AuthMethod.PASSWORD}),
    )
    return {"Authorization": f"Bearer {create_access_token(_auth_settings(), principal).access_token}"}


def _auth_settings() -> AuthSettings:
    return AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
