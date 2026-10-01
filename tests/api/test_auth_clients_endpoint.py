from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.auth import AuthSettings, Scope, create_application_access_token
from app.core.auth.dependencies import get_auth_settings
from app.core.db.dependencies import get_db_session
from app.core.db.models import ApplicationClient, ApplicationClientStatus
from app.main import app

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def test_list_clients_returns_a_page(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_list(session, **kwargs):
        captured.update(kwargs)
        return [_client_model("integration")], 7

    monkeypatch.setattr("app.api.v1.auth.clients.list_application_clients", fake_list)

    async with _client() as client:
        response = await client.get("/api/v1/auth/clients", params={"limit": 1, "offset": 3}, headers=_auth_headers())

    assert response.status_code == 200
    body = response.json()
    assert (body["total"], body["limit"], body["offset"]) == (7, 1, 3)
    assert [item["client_id"] for item in body["items"]] == ["integration"]
    assert captured == {"limit": 1, "offset": 3}


async def test_list_clients_defaults_to_the_first_page(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_list(session, **kwargs):
        captured.update(kwargs)
        return [], 0

    monkeypatch.setattr("app.api.v1.auth.clients.list_application_clients", fake_list)

    async with _client() as client:
        response = await client.get("/api/v1/auth/clients", headers=_auth_headers())

    assert response.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}
    assert captured == {"limit": 50, "offset": 0}


async def test_list_clients_rejects_unknown_query_parameters():
    async with _client() as client:
        response = await client.get("/api/v1/auth/clients", params={"limt": 5}, headers=_auth_headers())

    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["errors"]] == [["query", "limt"]]


async def test_list_clients_requires_the_manage_scope():
    async with _client() as client:
        response = await client.get("/api/v1/auth/clients", headers=_auth_headers(Scope.AUTH_USERS_MANAGE))

    assert response.status_code == 403


def _client_model(client_id: str) -> ApplicationClient:
    return ApplicationClient(
        id=uuid4(),
        client_id=client_id,
        name=client_id.title(),
        description=None,
        status=ApplicationClientStatus.ACTIVE,
        secrets=[],
        scope_grants=[],
        created_at=_NOW,
        updated_at=_NOW,
    )


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


def _auth_headers(scope: Scope = Scope.AUTH_CLIENTS_MANAGE) -> dict[str, str]:
    token = create_application_access_token(
        _auth_settings(),
        principal_id=UUID("00000000-0000-0000-0000-000000000001"),
        client_id="admin",
        scopes=[str(scope)],
    )
    return {"Authorization": f"Bearer {token.access_token}"}


def _auth_settings() -> AuthSettings:
    return AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
