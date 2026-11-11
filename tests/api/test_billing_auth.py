"""Scope guards of the billing routes: reads need `billing:read`, writes `billing:write` (billing spec, decision X).

The cases run over every billing operation of the OpenAPI document, so a route added by a later slice is covered
without touching this file.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.api.v1.common import ErrorResponse
from app.core.auth import AuthSettings, Scope, create_application_access_token
from app.core.auth.dependencies import get_auth_settings
from app.core.db.dependencies import get_db_session
from app.main import app

HTTP_METHODS = {"get", "put", "post", "delete", "patch"}
PATH_VALUES = {
    "lesson_id": "00000000-0000-0000-0000-0000000000aa",
    "party_id": "00000000-0000-0000-0000-0000000000bb",
    "valid_from": "2026-09-01",
    "alias_id": "00000000-0000-0000-0000-0000000000cc",
}


def _billing_operations() -> list[tuple[str, str]]:
    return [
        (method.upper(), path.format(**PATH_VALUES))
        for path, item in app.openapi()["paths"].items()
        if path.startswith("/api/v1/billing/")
        for method in item
        if method in HTTP_METHODS
    ]


def _required_scope(method: str) -> Scope:
    return Scope.BILLING_READ if method == "GET" else Scope.BILLING_WRITE


def test_the_operation_table_holds_the_lesson_routes():
    assert set(_billing_operations()) >= {
        ("GET", "/api/v1/billing/lessons"),
        ("POST", "/api/v1/billing/lessons"),
        ("GET", f"/api/v1/billing/lessons/{PATH_VALUES['lesson_id']}"),
        ("PATCH", f"/api/v1/billing/lessons/{PATH_VALUES['lesson_id']}"),
    }


def test_the_operation_table_holds_the_rate_routes():
    rates = f"/api/v1/billing/students/{PATH_VALUES['party_id']}/rates"

    assert set(_billing_operations()) >= {
        ("GET", rates),
        ("PUT", f"{rates}/{PATH_VALUES['valid_from']}"),
        ("DELETE", f"{rates}/{PATH_VALUES['valid_from']}"),
    }


def test_the_operation_table_holds_the_payer_routes():
    party = f"/api/v1/billing/parties/{PATH_VALUES['party_id']}"

    assert set(_billing_operations()) >= {
        ("GET", f"/api/v1/billing/students/{PATH_VALUES['party_id']}/payer"),
        ("POST", f"/api/v1/billing/lessons/{PATH_VALUES['lesson_id']}/resolve-payer"),
        ("PUT", f"{party}/sevdesk-contact"),
        ("DELETE", f"{party}/sevdesk-contact"),
        ("PUT", f"{party}/manual-billing"),
        ("DELETE", f"{party}/manual-billing"),
    }


def test_the_operation_table_holds_the_calendar_alias_routes():
    aliases = "/api/v1/billing/calendar/aliases"

    assert set(_billing_operations()) >= {
        ("GET", aliases),
        ("POST", aliases),
        ("DELETE", f"{aliases}/{PATH_VALUES['alias_id']}"),
    }


@pytest.mark.parametrize(("method", "path"), _billing_operations())
async def test_billing_route_answers_401_without_a_token(method: str, path: str):
    async with _client() as client:
        response = await client.request(method, path)

    assert response.status_code == 401
    assert ErrorResponse.model_validate(response.json()).code == "unauthorized"
    assert response.headers["www-authenticate"] == f'Bearer scope="{_required_scope(method)}"'


@pytest.mark.parametrize(("method", "path"), _billing_operations())
async def test_billing_route_answers_403_with_a_token_lacking_the_scope(method: str, path: str):
    """The other billing scope does not stand in: `billing:write` does not imply `billing:read`, nor the reverse."""
    other_billing_scope = Scope.BILLING_WRITE if method == "GET" else Scope.BILLING_READ

    async with _client() as client:
        response = await client.request(
            method, path, headers=_auth_headers(other_billing_scope, Scope.CRM_READ, Scope.CRM_WRITE)
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "Not enough permissions", "code": "forbidden"}


async def _override_db_session() -> AsyncIterator[object]:
    yield object()


@asynccontextmanager
async def _client() -> AsyncGenerator[AsyncClient]:
    app.dependency_overrides[get_db_session] = _override_db_session
    app.dependency_overrides[get_auth_settings] = lambda: _auth_settings()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
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


def _auth_settings() -> AuthSettings:
    return AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
