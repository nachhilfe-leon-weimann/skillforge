"""Pins status, ``detail`` and ``code`` of every error the application-client routes return.

Each case stubs the service function an endpoint calls, makes it raise with an *internal* instance
message, and asserts what a client sees.
"""

from __future__ import annotations

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
from app.main import app
from app.services.auth import (
    ApplicationClientAlreadyExistsError,
    ApplicationClientNotFoundError,
    ApplicationClientScopeGrantNotFoundError,
    ApplicationClientSecretNotFoundError,
    InvalidClientScopeError,
)

ID = "00000000-0000-0000-0000-0000000000aa"
INTERNAL = "internal: row 7f3a"
CLIENT_NOT_FOUND = "Application client not found"


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    stub: str
    """The service function the endpoint calls, by its name in ``app.api.v1.auth.clients``."""
    json: dict[str, Any] | None = None


ENDPOINTS = {
    "create_client": Endpoint("POST", "", "create_application_client", json={"client_id": "x", "name": "X"}),
    "read_client": Endpoint("GET", "/x", "get_application_client"),
    "update_client": Endpoint("PATCH", "/x", "update_application_client", json={"name": "Y"}),
    "create_secret": Endpoint("POST", "/x/secrets", "create_application_client_secret", json={}),
    "revoke_secret": Endpoint("DELETE", f"/x/secrets/{ID}", "revoke_application_client_secret"),
    "grant_scopes": Endpoint(
        "POST",
        "/x/scopes",
        "grant_application_client_scopes",
        json={"scopes": ["auth:clients:manage"], "mode": "application"},
    ),
    "revoke_scope": Endpoint("DELETE", "/x/scopes/application/auth:clients:manage", "revoke_application_client_scope"),
}

# (endpoint, raised error, status, detail, code)
type Expectation = tuple[str, Exception, int, str, str]

EXPECTATIONS: list[Expectation] = [
    (
        "create_client",
        ApplicationClientAlreadyExistsError(INTERNAL),
        409,
        "Application client already exists",
        "application_client_already_exists",
    ),
    ("read_client", ApplicationClientNotFoundError(INTERNAL), 404, CLIENT_NOT_FOUND, "application_client_not_found"),
    ("update_client", ApplicationClientNotFoundError(INTERNAL), 404, CLIENT_NOT_FOUND, "application_client_not_found"),
    ("create_secret", ApplicationClientNotFoundError(INTERNAL), 404, CLIENT_NOT_FOUND, "application_client_not_found"),
    ("revoke_secret", ApplicationClientNotFoundError(INTERNAL), 404, CLIENT_NOT_FOUND, "application_client_not_found"),
    (
        "revoke_secret",
        ApplicationClientSecretNotFoundError(INTERNAL),
        404,
        "Application client secret not found",
        "application_client_secret_not_found",
    ),
    ("grant_scopes", ApplicationClientNotFoundError(INTERNAL), 404, CLIENT_NOT_FOUND, "application_client_not_found"),
    ("grant_scopes", InvalidClientScopeError(INTERNAL), 400, "Invalid requested scope", "invalid_scope"),
    ("revoke_scope", ApplicationClientNotFoundError(INTERNAL), 404, CLIENT_NOT_FOUND, "application_client_not_found"),
    (
        "revoke_scope",
        ApplicationClientScopeGrantNotFoundError(INTERNAL),
        404,
        "Application client scope grant not found",
        "application_client_scope_grant_not_found",
    ),
]


def _expectation_id(expectation: Expectation) -> str:
    name, error, *_ = expectation
    return f"{name}-{type(error).__name__}"


@pytest.mark.parametrize("expectation", EXPECTATIONS, ids=_expectation_id)
async def test_auth_client_error_status_and_detail(expectation: Expectation, monkeypatch):
    name, error, status, detail, code = expectation
    endpoint = ENDPOINTS[name]
    monkeypatch.setattr(f"app.api.v1.auth.clients.{endpoint.stub}", _raises(error))

    async with _client() as client:
        response = await client.request(
            endpoint.method,
            f"/api/v1/auth/clients{endpoint.path}",
            json=endpoint.json,
            headers=_auth_headers(),
        )

    assert response.status_code == status
    assert response.json() == {"detail": detail, "code": code}
    assert INTERNAL not in response.text


def test_every_error_of_the_table_is_documented_on_its_route():
    paths = app.openapi()["paths"]

    for name, _, status, detail, code in EXPECTATIONS:
        endpoint = ENDPOINTS[name]
        operation = paths[f"/api/v1/auth/clients{_template(endpoint.path)}"][endpoint.method.lower()]
        examples = operation["responses"][str(status)]["content"]["application/json"]["examples"]
        assert examples[code]["value"] == {"detail": detail, "code": code}, name


def _template(path: str) -> str:
    return (
        path
        .replace("/x", "/{client_id}", 1)
        .replace(ID, "{secret_id}")
        .replace("application/auth:clients:manage", "{mode}/{scope_key}")
    )


def test_every_endpoint_of_the_table_is_exercised():
    assert {name for name, *_ in EXPECTATIONS} == set(ENDPOINTS)


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
        client_id="admin",
        scopes=[str(Scope.AUTH_CLIENTS_MANAGE)],
    )
    return {"Authorization": f"Bearer {token.access_token}"}


def _auth_settings() -> AuthSettings:
    return AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
