"""The client routes take the grant mode (ADR 0008): in the grant body, in the revoke path, per list in the answer."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.api.v1.auth.schemas import ApplicationClientResponse
from app.core.auth import AuthSettings, Scope, create_application_access_token
from app.core.auth.dependencies import get_auth_settings
from app.core.db.dependencies import get_db_session
from app.core.db.models import ApplicationClient, ApplicationClientScopeGrant, ApplicationClientStatus, GrantMode
from app.main import app

CLIENTS = "/api/v1/auth/clients"
GRANT = f"{CLIENTS}/{{client_id}}/scopes"
REVOKE = f"{CLIENTS}/{{client_id}}/scopes/{{mode}}/{{scope_key}}"
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return app.openapi()


async def test_the_grant_hands_the_mode_to_the_service(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_grant(session, **kwargs):
        captured.update(kwargs)
        return _client_model(("crm:read", GrantMode.DELEGATED))

    monkeypatch.setattr("app.api.v1.auth.clients.grant_application_client_scopes", fake_grant)

    async with _client() as client:
        response = await client.post(f"{CLIENTS}/portal/scopes", json={"scopes": ["crm:read"], "mode": "delegated"})

    assert response.status_code == 200
    assert captured == {"client_id": "portal", "scopes": ["crm:read"], "mode": GrantMode.DELEGATED}
    assert (response.json()["application_scopes"], response.json()["delegated_scopes"]) == ([], ["crm:read"])


async def test_the_revoke_hands_the_mode_of_its_path_to_the_service(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_revoke(session, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("app.api.v1.auth.clients.revoke_application_client_scope", fake_revoke)

    async with _client() as client:
        response = await client.delete(f"{CLIENTS}/portal/scopes/delegated/crm:read")

    assert response.status_code == 204
    assert captured == {"client_id": "portal", "scope_key": "crm:read", "mode": GrantMode.DELEGATED}


@pytest.mark.parametrize(
    ("body", "loc"),
    [
        ({"scopes": ["crm:read"]}, ["body", "mode"]),
        ({"scopes": ["crm:read"], "mode": "both"}, ["body", "mode"]),
    ],
)
async def test_a_grant_without_a_known_mode_is_a_validation_error(body: dict[str, Any], loc: list[str]):
    async with _client() as client:
        response = await client.post(f"{CLIENTS}/portal/scopes", json=body)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert [error["loc"] for error in response.json()["errors"]] == [loc]


async def test_a_revoke_in_an_unknown_mode_is_a_validation_error():
    async with _client() as client:
        response = await client.delete(f"{CLIENTS}/portal/scopes/both/crm:read")

    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["errors"]] == [["path", "mode"]]


def test_the_client_lists_its_grants_per_mode_each_sorted():
    client = _client_model(
        ("crm:write", GrantMode.APPLICATION),
        ("crm:read", GrantMode.DELEGATED),
        ("auth:users:login", GrantMode.APPLICATION),
        ("crm:write", GrantMode.DELEGATED),
        ("account:self", GrantMode.DELEGATED),
    )

    response = ApplicationClientResponse.from_model(client)

    assert response.application_scopes == ["auth:users:login", "crm:write"]
    assert response.delegated_scopes == ["account:self", "crm:read", "crm:write"]


def test_the_grant_body_requires_a_mode_of_the_two(schema: dict[str, Any]):
    request = schema["components"]["schemas"]["ApplicationClientScopeGrantRequest"]

    assert request["required"] == ["scopes", "mode"]
    assert request["properties"]["mode"]["$ref"] == "#/components/schemas/GrantMode"
    assert schema["components"]["schemas"]["GrantMode"]["enum"] == ["application", "delegated"]


def test_the_client_answer_has_a_scope_list_per_mode(schema: dict[str, Any]):
    properties = schema["components"]["schemas"]["ApplicationClientResponse"]["properties"]

    assert {"application_scopes", "delegated_scopes"} <= set(properties)
    assert "scopes" not in properties


@pytest.mark.parametrize("name", ["ApplicationClientScopeGrantRequest", "ApplicationClientResponse"])
def test_every_property_of_the_changed_schemas_is_described(schema: dict[str, Any], name: str):
    for property_name, definition in schema["components"]["schemas"][name]["properties"].items():
        assert definition.get("description", "").strip(), f"{name}.{property_name}"


def test_the_revoke_path_names_the_mode(schema: dict[str, Any]):
    paths = schema["paths"]
    operation = paths[REVOKE]["delete"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

    assert f"{CLIENTS}/{{client_id}}/scopes/{{scope_key}}" not in paths
    assert operation["operationId"] == "auth_revoke_application_client_scope"
    assert list(parameters) == ["client_id", "mode", "scope_key"]
    assert parameters["mode"]["schema"]["$ref"] == "#/components/schemas/GrantMode"
    assert parameters["mode"]["description"].strip()
    assert parameters["scope_key"]["description"].strip()


def test_the_grant_keeps_its_operation(schema: dict[str, Any]):
    assert schema["paths"][GRANT]["post"]["operationId"] == "auth_grant_application_client_scopes"


def _client_model(*grants: tuple[str, GrantMode]) -> ApplicationClient:
    return ApplicationClient(
        id=uuid4(),
        client_id="portal",
        name="Portal",
        description=None,
        status=ApplicationClientStatus.ACTIVE,
        secrets=[],
        scope_grants=[ApplicationClientScopeGrant(scope_key=scope_key, mode=mode) for scope_key, mode in grants],
        created_at=_NOW,
        updated_at=_NOW,
    )


async def _override_db_session() -> AsyncIterator[object]:
    yield object()


@asynccontextmanager
async def _client() -> AsyncGenerator[AsyncClient]:
    app.dependency_overrides[get_db_session] = _override_db_session
    app.dependency_overrides[get_auth_settings] = lambda: _auth_settings()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver", headers=_auth_headers()
        ) as client:
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
