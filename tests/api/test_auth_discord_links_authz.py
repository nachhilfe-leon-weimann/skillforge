"""Runtime guards of the Discord link routes: reads need `auth:discord-links:read`, writes `auth:users:manage`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

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
from app.main import app

SETTINGS = AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
HTTP_METHODS = {"get", "put", "post", "delete", "patch"}
READ = "auth:discord-links:read"
WRITE = "auth:users:manage"
LINK_ID = "123456789012345678"
BODIES = {("PUT", f"/api/v1/auth/discord-links/{LINK_ID}"): {"party_id": str(uuid4())}}


def _link_operations() -> list[tuple[str, str, str]]:
    """Every `/auth/discord-links` operation with the scope it needs."""
    return [
        (method.upper(), path.format(discord_user_id=LINK_ID), READ if method == "get" else WRITE)
        for path, item in app.openapi()["paths"].items()
        if path.startswith("/api/v1/auth/discord-links")
        for method in item
        if method in HTTP_METHODS
    ]


def test_the_operation_table_covers_the_link_surface():
    assert len(_link_operations()) == 4


@pytest.mark.parametrize(("method", "path", "scope"), _link_operations())
async def test_a_link_route_answers_401_without_a_token(method: str, path: str, scope: str):
    async with _client() as client:
        response = await client.request(method, path, json=BODIES.get((method, path)))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == f'Bearer scope="{scope}"'


@pytest.mark.parametrize(("method", "path", "scope"), _link_operations())
async def test_a_link_route_is_403_for_an_application_without_its_scope(method: str, path: str, scope: str):
    headers = _application_headers(*(member for member in Scope if member != scope))
    async with _client() as client:
        response = await client.request(method, path, json=BODIES.get((method, path)), headers=headers)

    assert response.status_code == 403
    assert response.json() == {"detail": "Not enough permissions", "code": "forbidden"}


@pytest.mark.parametrize(("method", "path", "scope"), _link_operations())
async def test_a_link_route_is_403_for_a_persons_token_without_its_scope(method: str, path: str, scope: str):
    other = Scope.AUTH_USERS_MANAGE if scope == READ else Scope.AUTH_DISCORD_LINKS_READ
    async with _client() as client:
        response = await client.request(
            method, path, json=BODIES.get((method, path)), headers=_person_headers(Scope.ACCOUNT_SELF, other)
        )

    assert response.status_code == 403


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
        session_id=uuid4(),
        roles=frozenset(),
        auth_methods=frozenset({AuthMethod.PASSWORD}),
    )
    return {"Authorization": f"Bearer {create_access_token(SETTINGS, principal).access_token}"}
