from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.auth import AuthSettings, Scope, create_application_access_token
from app.core.auth.dependencies import get_auth_settings
from app.core.db.dependencies import get_db_session
from app.core.db.models import DiscordUser, DiscordUserPermissionGroup, MemberRole
from app.main import app
from app.services.bot import (
    GroupMembershipNotFoundError,
    PermissionGroupNotFoundError,
    PrincipalNotFoundError,
)

# --- upsert user ------------------------------------------------------------


async def test_upsert_user_returns_user(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_upsert(session, **kwargs):
        captured.update(kwargs)
        return DiscordUser(discord_id=42, role=MemberRole.TUTOR, nick_name="Tutor", active=True)

    _patch(monkeypatch, "upsert_discord_user", fake_upsert)

    async with _client() as client:
        response = await client.put(
            "/api/v1/bot/users/42",
            json={"role": "tutor", "nick_name": "Tutor", "active": True},
            headers=_auth_headers(Scope.BOT_WRITE),
        )

    assert response.status_code == 200
    assert response.json() == {"discord_id": 42, "role": "tutor", "nick_name": "Tutor", "active": True}
    assert captured == {"discord_id": 42, "role": MemberRole.TUTOR, "nick_name": "Tutor", "active": True}


async def test_upsert_user_requires_bot_write_scope():
    async with _client() as client:
        response = await client.put(
            "/api/v1/bot/users/42",
            json={"role": "tutor", "nick_name": "Tutor"},
            headers=_auth_headers(Scope.BOT_READ),
        )

    assert response.status_code == 403


async def test_upsert_user_rejects_empty_nick():
    async with _client() as client:
        response = await client.put(
            "/api/v1/bot/users/42",
            json={"role": "tutor", "nick_name": ""},
            headers=_auth_headers(Scope.BOT_WRITE),
        )

    assert response.status_code == 422


# --- group membership -------------------------------------------------------


async def test_add_group_returns_membership(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_add(session, **kwargs):
        captured.update(kwargs)
        return DiscordUserPermissionGroup(discord_id=42, group_key="support")

    _patch(monkeypatch, "add_user_to_group", fake_add)

    async with _client() as client:
        response = await client.put("/api/v1/bot/users/42/groups/support", headers=_auth_headers(Scope.BOT_WRITE))

    assert response.status_code == 200
    assert response.json() == {"discord_id": 42, "group_key": "support"}
    assert captured == {"discord_id": 42, "group_key": "support"}


async def test_add_group_requires_bot_write_scope():
    async with _client() as client:
        response = await client.put("/api/v1/bot/users/42/groups/support", headers=_auth_headers(Scope.BOT_READ))

    assert response.status_code == 403


async def test_add_group_returns_404_for_unknown_user(monkeypatch):
    _patch(monkeypatch, "add_user_to_group", _raises(PrincipalNotFoundError()))

    async with _client() as client:
        response = await client.put("/api/v1/bot/users/42/groups/support", headers=_auth_headers(Scope.BOT_WRITE))

    assert response.status_code == 404


async def test_add_group_returns_404_for_unknown_group(monkeypatch):
    _patch(monkeypatch, "add_user_to_group", _raises(PermissionGroupNotFoundError()))

    async with _client() as client:
        response = await client.put("/api/v1/bot/users/42/groups/nope", headers=_auth_headers(Scope.BOT_WRITE))

    assert response.status_code == 404


async def test_remove_group_returns_204(monkeypatch):
    _patch(monkeypatch, "remove_user_from_group", _returns(None))

    async with _client() as client:
        response = await client.delete("/api/v1/bot/users/42/groups/support", headers=_auth_headers(Scope.BOT_WRITE))

    assert response.status_code == 204


async def test_remove_group_returns_404(monkeypatch):
    _patch(monkeypatch, "remove_user_from_group", _raises(GroupMembershipNotFoundError()))

    async with _client() as client:
        response = await client.delete("/api/v1/bot/users/42/groups/support", headers=_auth_headers(Scope.BOT_WRITE))

    assert response.status_code == 404


# --- helpers ----------------------------------------------------------------


def _returns(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


def _patch(monkeypatch, name: str, replacement) -> None:
    monkeypatch.setattr(f"app.api.v1.bot.users.{name}", replacement)


def _raises(error: Exception):
    async def _inner(*args, **kwargs):
        raise error

    return _inner


async def _override_db_session() -> AsyncIterator[object]:
    yield object()


@asynccontextmanager
async def _client() -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_db_session] = _override_db_session
    app.dependency_overrides[get_auth_settings] = _auth_settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def _auth_headers(*scopes: Scope) -> dict[str, str]:
    token = create_application_access_token(
        _auth_settings(),
        principal_id=UUID("00000000-0000-0000-0000-000000000001"),
        client_id="skillbot",
        scopes=[str(scope) for scope in scopes],
    )
    return {"Authorization": f"Bearer {token.access_token}"}


def _auth_settings() -> AuthSettings:
    return AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
