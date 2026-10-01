"""Every non-2xx response of the real app carries the error envelope (ADR 0006)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.api.v1.common import ErrorResponse
from app.core.auth import AuthSettings, Scope, create_application_access_token
from app.core.auth.dependencies import get_auth_settings
from app.core.db.dependencies import get_db_session
from app.core.errors import NotFoundError
from app.main import app
from app.services.auth import users as users_service

USERS = "/api/v1/auth/users"


class ProbeNotFoundError(NotFoundError):
    message = "Probe not found"


@dataclass(frozen=True)
class Case:
    path: str
    status: int
    code: str
    scopes: tuple[Scope, ...] | None = None
    params: dict[str, str] = field(default_factory=dict)
    raises: Exception | None = None


CASES = {
    "mapped_domain_error": Case(
        path=f"{USERS}/{uuid4()}",
        status=404,
        code="probe_not_found",
        scopes=(Scope.AUTH_USERS_MANAGE,),
        raises=ProbeNotFoundError("probe 7f3a is gone"),
    ),
    "unknown_route": Case(path="/api/v1/nope", status=404, code="not_found"),
    "missing_token": Case(path=USERS, status=401, code="unauthorized"),
    "missing_scope": Case(path=USERS, status=403, code="forbidden", scopes=(Scope.AUTH_CLIENTS_MANAGE,)),
    "malformed_path_parameter": Case(
        path=f"{USERS}/not-a-uuid",
        status=422,
        code="validation_error",
        scopes=(Scope.AUTH_USERS_MANAGE,),
    ),
    "malformed_query_parameter": Case(
        path=USERS,
        status=422,
        code="validation_error",
        scopes=(Scope.AUTH_USERS_MANAGE,),
        params={"limit": "0"},
    ),
}


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
async def test_error_body_is_the_envelope(case: Case, monkeypatch):
    if case.raises is not None:
        monkeypatch.setattr(users_service, "load_user_account", _raises(case.raises))

    async with _client() as client:
        response = await client.get(case.path, params=case.params, headers=_auth_headers(case.scopes))

    assert response.status_code == case.status
    envelope = ErrorResponse.model_validate(response.json())
    assert envelope.code == case.code
    assert envelope.detail
    assert "7f3a" not in response.text
    if case.code == "validation_error":
        assert envelope.errors
    else:
        assert "errors" not in response.json()


async def test_missing_token_keeps_the_www_authenticate_challenge():
    async with _client() as client:
        response = await client.get(USERS)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer scope="auth:users:manage"'


async def test_unexpected_exception_is_a_500_envelope_without_internals(monkeypatch):
    monkeypatch.setattr(users_service, "load_user_account", _raises(RuntimeError("connection string leaked: 7f3a")))

    async with _client(raise_app_exceptions=False) as client:
        response = await client.get(f"{USERS}/{uuid4()}", headers=_auth_headers((Scope.AUTH_USERS_MANAGE,)))

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error", "code": "internal_error"}
    assert response.headers["x-request-id"]


@pytest.mark.parametrize("failure", ["missing_token", "invalid_token", "missing_scope"])
async def test_documented_auth_error_examples_are_the_bodies_the_api_returns(failure: str):
    headers = {
        "missing_token": {},
        "invalid_token": {"Authorization": "Bearer not-a-token"},
        "missing_scope": _auth_headers((Scope.AUTH_CLIENTS_MANAGE,)),
    }[failure]

    async with _client() as client:
        response = await client.get(USERS, headers=headers)

    assert response.json() == _documented_auth_example(USERS, failure)


def _documented_auth_example(path: str, failure: str) -> dict[str, str]:
    responses = app.openapi()["paths"][path]["get"]["responses"]
    if failure == "missing_scope":
        return responses["403"]["content"]["application/json"]["examples"]["forbidden"]["value"]

    return responses["401"]["content"]["application/json"]["examples"][failure]["value"]


def _raises(error: Exception):
    async def _inner(*args, **kwargs):
        raise error

    return _inner


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


def _auth_headers(scopes: tuple[Scope, ...] | None) -> dict[str, str]:
    if scopes is None:
        return {}

    # An operator tool, not skillbot: the skillbot client never holds `auth:*` (bot-decoupling, "Security rules").
    token = create_application_access_token(
        _auth_settings(),
        principal_id=UUID("00000000-0000-0000-0000-000000000001"),
        client_id="operator",
        scopes=[str(scope) for scope in scopes],
    )
    return {"Authorization": f"Bearer {token.access_token}"}


def _auth_settings() -> AuthSettings:
    return AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
