"""The Discord link routes over their service seam: status mapping, the wire type, the actor (no database)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.auth import AuthSettings, Scope, create_application_access_token
from app.core.auth.dependencies import get_auth_settings
from app.core.db.dependencies import get_db_session
from app.core.db.models import DiscordAccount
from app.main import app
from app.services.auth import discord_links as discord_links_service
from app.services.auth.errors import (
    DiscordAccountAlreadyLinkedError,
    DiscordLinkNotFoundError,
    LinkPartyNotAPersonError,
    UnknownLinkPartyError,
)

SETTINGS = AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
SNOWFLAKE = 123456789012345678
PATH = f"/api/v1/auth/discord-links/{SNOWFLAKE}"
PARTY_ID = UUID("11111111-1111-1111-1111-111111111111")
STAMP = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)


def _link() -> DiscordAccount:
    link = DiscordAccount(discord_id=SNOWFLAKE, party_id=PARTY_ID, active=True, is_primary=False)
    link.created_at = link.updated_at = STAMP
    return link


async def test_put_passes_the_id_the_party_and_the_actor_and_answers_the_link_as_a_string(monkeypatch):
    calls: list[dict[str, object]] = []

    async def fake(session: object, **kwargs: object) -> DiscordAccount:
        calls.append(kwargs)
        return _link()

    monkeypatch.setattr(discord_links_service, "link_discord_account", fake)
    async with _client() as client:
        response = await client.put(PATH, json={"party_id": str(PARTY_ID)}, headers=_headers(Scope.AUTH_USERS_MANAGE))

    assert response.status_code == 200
    assert response.json() == {
        "discord_user_id": str(SNOWFLAKE),
        "party_id": str(PARTY_ID),
        "active": True,
        "created_at": "2026-09-25T08:00:00Z",
        "updated_at": "2026-09-25T08:00:00Z",
    }
    assert calls[0]["discord_user_id"] == SNOWFLAKE and calls[0]["party_id"] == PARTY_ID
    assert getattr(calls[0]["actor"], "client_id") == "operator"  # noqa: B009 (ty: `actor` is typed `object`)


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (UnknownLinkPartyError(), 422, "unknown_link_party"),
        (LinkPartyNotAPersonError(), 422, "link_party_not_a_person"),
        (DiscordAccountAlreadyLinkedError(), 409, "discord_account_already_linked"),
    ],
)
async def test_put_maps_the_link_errors(monkeypatch, error: Exception, status: int, code: str):
    async def fake(session: object, **kwargs: object) -> DiscordAccount:
        raise error

    monkeypatch.setattr(discord_links_service, "link_discord_account", fake)
    async with _client() as client:
        response = await client.put(PATH, json={"party_id": str(uuid4())}, headers=_headers(Scope.AUTH_USERS_MANAGE))

    assert (response.status_code, response.json()["code"]) == (status, code)


@pytest.mark.parametrize(("method", "fake_name"), [("DELETE", "unlink_discord_account"), ("GET", "get_discord_link")])
async def test_an_unknown_link_is_404(monkeypatch, method: str, fake_name: str):
    async def fake(session: object, *args: object, **kwargs: object) -> None:
        raise DiscordLinkNotFoundError()

    monkeypatch.setattr(discord_links_service, fake_name, fake)
    headers = _headers(Scope.AUTH_USERS_MANAGE, Scope.AUTH_DISCORD_LINKS_READ)
    async with _client() as client:
        response = await client.request(method, PATH, headers=headers)

    assert (response.status_code, response.json()["code"]) == (404, "discord_link_not_found")


async def test_delete_answers_204(monkeypatch):
    async def fake(session: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(discord_links_service, "unlink_discord_account", fake)
    async with _client() as client:
        response = await client.delete(PATH, headers=_headers(Scope.AUTH_USERS_MANAGE))

    assert response.status_code == 204


@pytest.mark.parametrize("bad", ["-1", "+5", str(2**63), "1e3", "abc"])
async def test_a_malformed_discord_user_id_is_422_and_never_reaches_the_service(monkeypatch, bad: str):
    async def fake(session: object, **kwargs: object) -> DiscordAccount:
        raise AssertionError("the service must not run")

    monkeypatch.setattr(discord_links_service, "link_discord_account", fake)
    async with _client() as client:
        response = await client.put(
            f"/api/v1/auth/discord-links/{bad}",
            json={"party_id": str(uuid4())},
            headers=_headers(Scope.AUTH_USERS_MANAGE),
        )

    assert (response.status_code, response.json()["code"]) == (422, "validation_error")


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


def _headers(*scopes: Scope) -> dict[str, str]:
    token = create_application_access_token(
        SETTINGS, principal_id=UUID("00000000-0000-0000-0000-000000000001"), client_id="operator", scopes=scopes
    )
    return {"Authorization": f"Bearer {token.access_token}"}
