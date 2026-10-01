from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.api.v1.auth.token import get_issue_client_token
from app.core.auth import AuthSettings, CreatedAccessToken
from app.core.auth.dependencies import get_auth_settings
from app.core.db.dependencies import get_db_session
from app.main import app
from app.services.auth import InvalidClientCredentialsError, InvalidClientScopeError


async def test_auth_token_endpoint_returns_access_token():
    captured: dict[str, object] = {}

    async def fake_create_token(
        session,
        settings,
        *,
        client_id,
        client_secret,
        requested_scopes,
    ):
        captured.update({
            "session": session,
            "settings": settings,
            "client_id": client_id,
            "client_secret": client_secret,
            "requested_scopes": requested_scopes,
        })
        return _token(scope="auth:clients:manage")

    with _overrides(fake_create_token):
        response = await _post(
            "/api/v1/auth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "integration",
                "client_secret": "secret",
                "scope": "auth:clients:manage",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "access_token": "encoded-token",
        "token_type": "bearer",
        "expires_in": 900,
        "scope": "auth:clients:manage",
    }
    assert captured["session"] == "session"
    assert captured["client_id"] == "integration"
    assert captured["client_secret"] == "secret"
    assert captured["requested_scopes"] == "auth:clients:manage"


async def test_auth_token_endpoint_accepts_basic_client_credentials():
    captured: dict[str, object] = {}

    async def fake_create_token(
        session,
        settings,
        *,
        client_id,
        client_secret,
        requested_scopes,
    ):
        captured.update({
            "client_id": client_id,
            "client_secret": client_secret,
            "requested_scopes": requested_scopes,
        })
        return _token(scope="auth:clients:manage auth:users:manage")

    with _overrides(fake_create_token):
        response = await _post(
            "/api/v1/auth/token",
            auth=("integration", "secret"),
            data={
                "grant_type": "client_credentials",
                "scope": "auth:clients:manage auth:users:manage",
            },
        )

    assert response.status_code == 200
    assert captured == {
        "client_id": "integration",
        "client_secret": "secret",
        "requested_scopes": "auth:clients:manage auth:users:manage",
    }


async def test_auth_token_endpoint_rejects_unsupported_grant_type():
    async def fake_create_token(*args, **kwargs):
        raise AssertionError("service should not be called")

    with _overrides(fake_create_token):
        response = await _post(
            "/api/v1/auth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "integration",
                "client_secret": "secret",
            },
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "Unsupported grant_type", "code": "unsupported_grant_type"}


async def test_auth_token_endpoint_rejects_invalid_client_credentials():
    async def fake_create_token(*args, **kwargs):
        raise InvalidClientCredentialsError("invalid")

    with _overrides(fake_create_token):
        response = await _post(
            "/api/v1/auth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "integration",
                "client_secret": "wrong",
            },
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Invalid client credentials", "code": "invalid_client"}


async def test_auth_token_endpoint_rejects_invalid_scope():
    async def fake_create_token(*args, **kwargs):
        raise InvalidClientScopeError("invalid scope")

    with _overrides(fake_create_token):
        response = await _post(
            "/api/v1/auth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "integration",
                "client_secret": "secret",
                "scope": "users:write",
            },
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid requested scope", "code": "invalid_scope"}


@pytest.mark.parametrize("denial", [InvalidClientCredentialsError("invalid"), InvalidClientScopeError("invalid scope")])
async def test_auth_token_denial_commits_the_session_so_the_audit_entry_survives(denial: Exception):
    # issue_client_token writes a TOKEN_DENIED audit entry before it raises. The endpoint must
    # return the error: an exception reaching the session dependency would roll the entry back.
    outcome: list[str] = []

    async def tracked_session():
        try:
            yield "session"
        except BaseException:
            outcome.append("rolled back")
            raise
        outcome.append("committed")

    async def fake_create_token(*args, **kwargs):
        raise denial

    with _overrides(fake_create_token):
        app.dependency_overrides[get_db_session] = tracked_session
        response = await _post(
            "/api/v1/auth/token",
            data={"grant_type": "client_credentials", "client_id": "integration", "client_secret": "wrong"},
        )

    assert response.status_code in {400, 401}
    assert outcome == ["committed"]


async def test_auth_token_endpoint_requires_form_fields():
    async def fake_create_token(*args, **kwargs):
        raise AssertionError("service should not be called")

    with _overrides(fake_create_token):
        response = await _post("/api/v1/auth/token", data={})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert [error["loc"] for error in response.json()["errors"]] == [["body", "grant_type"]]


async def test_auth_token_endpoint_requires_client_credentials():
    async def fake_create_token(*args, **kwargs):
        raise AssertionError("service should not be called")

    with _overrides(fake_create_token):
        response = await _post("/api/v1/auth/token", data={"grant_type": "client_credentials"})

    assert response.status_code == 422
    assert response.json() == {"detail": "A required parameter is missing", "code": "invalid_request"}


def test_auth_token_endpoint_documents_every_error_it_returns():
    responses = app.openapi()["paths"]["/api/v1/auth/token"]["post"]["responses"]

    assert set(responses) == {"200", "400", "401", "422"}
    assert set(responses["400"]["content"]["application/json"]["examples"]) == {
        "unsupported_grant_type",
        "invalid_scope",
        "invalid_grant",
        "unauthorized_client",
    }
    assert set(responses["401"]["content"]["application/json"]["examples"]) == {"invalid_client"}
    assert set(responses["422"]["content"]["application/json"]["examples"]) == {"invalid_request", "validation_error"}


class _overrides:
    def __init__(self, fake_create_token):
        self.fake_create_token = fake_create_token

    def __enter__(self):
        app.dependency_overrides[get_db_session] = lambda: "session"
        app.dependency_overrides[get_auth_settings] = lambda: AuthSettings(
            secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes")
        )
        app.dependency_overrides[get_issue_client_token] = lambda: self.fake_create_token
        return self

    def __exit__(self, exc_type, exc, tb):
        app.dependency_overrides.clear()


async def _post(path: str, **kwargs: Any) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        return await client.post(path, **kwargs)


def _token(*, scope: str) -> CreatedAccessToken:
    now = datetime.now(UTC)
    return CreatedAccessToken(
        access_token="encoded-token",
        token_type="bearer",
        expires_at=now + timedelta(minutes=15),
        expires_in=900,
        scope=scope,
    )
