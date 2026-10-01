"""Runtime guards of the account routes and the redeem route: 401 without a token, 403 without the right principal.

The cases run over every `/auth/users` operation of the OpenAPI document, so a route added later is covered
without touching this file. No database: every request is refused before a service runs.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.auth import (
    AuthSettings,
    PasswordLogin,
    Principal,
    Scope,
    UserPrincipal,
    create_access_token,
    create_application_access_token,
)
from app.core.auth import dependencies as auth_dependencies
from app.core.auth.dependencies import get_auth_settings
from app.core.auth.roles import Role
from app.core.auth.scopes import CLIENT_ONLY_SCOPES
from app.core.db.dependencies import get_db_session
from app.main import app
from app.services.auth import action_tokens as action_tokens_service

SETTINGS = AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
HTTP_METHODS = {"get", "put", "post", "delete", "patch"}
REDEEM_PATH = "/api/v1/auth/password/redeem"
REDEEM_BODY = {"token": "sf_ua_whatever", "new_password": "correct horse battery staple"}
USER_ID = "00000000-0000-0000-0000-0000000000dd"
BODIES: dict[tuple[str, str], dict[str, object]] = {
    ("POST", "/api/v1/auth/users"): {"party_id": str(uuid4()), "email": "anna@example.org"},
    ("PATCH", f"/api/v1/auth/users/{USER_ID}"): {"status": "disabled"},
}


def _account_operations() -> list[tuple[str, str]]:
    return [
        (method.upper(), path.format(user_id=USER_ID, role="admin"))
        for path, item in app.openapi()["paths"].items()
        if path.startswith("/api/v1/auth/users")
        for method in item
        if method in HTTP_METHODS
    ]


def test_the_operation_table_covers_the_whole_account_surface():
    assert len(_account_operations()) == 10


@pytest.mark.parametrize(("method", "path"), _account_operations())
async def test_an_account_route_answers_401_without_a_token(method: str, path: str):
    async with _client() as client:
        response = await client.request(method, path, json=BODIES.get((method, path)))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer scope="auth:users:manage"'


@pytest.mark.parametrize(("method", "path"), _account_operations())
async def test_an_account_route_is_403_without_auth_users_manage(method: str, path: str):
    headers = _application_headers(*(scope for scope in Scope if scope is not Scope.AUTH_USERS_MANAGE))
    async with _client() as client:
        response = await client.request(method, path, json=BODIES.get((method, path)), headers=headers)

    assert response.status_code == 403
    assert response.json() == {"detail": "Not enough permissions", "code": "forbidden"}


@pytest.mark.parametrize(("method", "path"), _account_operations())
async def test_an_account_route_is_403_for_a_persons_token_without_auth_users_manage(method: str, path: str):
    async with _client() as client:
        response = await client.request(
            method, path, json=BODIES.get((method, path)), headers=_person_headers(Scope.ACCOUNT_SELF, Scope.CRM_READ)
        )

    assert response.status_code == 403


async def test_the_redeem_route_is_403_for_a_client_without_auth_users_login():
    async with _client() as client:
        response = await client.post(
            REDEEM_PATH,
            json=REDEEM_BODY,
            headers=_application_headers(*(scope for scope in Scope if scope is not Scope.AUTH_USERS_LOGIN)),
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "Not enough permissions", "code": "forbidden"}


async def test_the_redeem_route_is_403_for_a_persons_token_whatever_its_scopes():
    """A signed person's token cannot carry `auth:users:login`, so the principal is built without a token."""

    async def person() -> Principal:
        return UserPrincipal(
            principal_id=uuid4(),
            client_id="portal",
            scopes=frozenset(Scope),
            party_id=uuid4(),
            roles=frozenset({Role.ADMIN}),
            login=PasswordLogin(session_id=uuid4()),
        )

    async with _client() as client:
        app.dependency_overrides[auth_dependencies.get_current_principal] = person
        response = await client.post(REDEEM_PATH, json=REDEEM_BODY)
        signed = await client.post(
            REDEEM_PATH, json=REDEEM_BODY, headers=_person_headers(*(frozenset(Scope) - CLIENT_ONLY_SCOPES))
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "Application principal required", "code": "forbidden"}
    assert signed.status_code == 403


async def test_the_redeem_route_validates_the_token_once(monkeypatch):
    """One guard declaration: the scope and the principal type are checked on a single validation."""
    validate = auth_dependencies.validate_access_token
    validations: list[str] = []

    def counting(token: str, settings: AuthSettings) -> Principal:
        validations.append(token)
        return validate(token, settings)

    async def redeemed(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(auth_dependencies, "validate_access_token", counting)
    monkeypatch.setattr(action_tokens_service, "redeem_action_token", redeemed)
    async with _client() as client:
        response = await client.post(
            REDEEM_PATH, json=REDEEM_BODY, headers=_application_headers(Scope.AUTH_USERS_LOGIN)
        )

    assert response.status_code == 204
    assert len(validations) == 1


async def _no_db_session() -> AsyncIterator[object]:
    yield object()


@asynccontextmanager
async def _client() -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_db_session] = _no_db_session
    app.dependency_overrides[get_auth_settings] = lambda: SETTINGS
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def _application_headers(*scopes: Scope) -> dict[str, str]:
    token = create_application_access_token(
        SETTINGS, principal_id=UUID("00000000-0000-0000-0000-000000000001"), client_id="operator", scopes=scopes
    )
    return {"Authorization": f"Bearer {token.access_token}"}


def _person_headers(*scopes: Scope) -> dict[str, str]:
    principal = UserPrincipal(
        principal_id=uuid4(),
        client_id="portal",
        scopes=frozenset(scopes),
        party_id=uuid4(),
        roles=frozenset(),
        login=PasswordLogin(session_id=uuid4()),
    )
    return {"Authorization": f"Bearer {create_access_token(SETTINGS, principal).access_token}"}
