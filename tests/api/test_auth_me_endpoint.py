"""`GET /auth/me`: what the calling token says about its bearer - without asking the database."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.api.v1.auth.me import get_me
from app.api.v1.common import ErrorResponse
from app.core.auth import (
    AuthSettings,
    PasswordLogin,
    Scope,
    UserPrincipal,
    create_access_token,
    create_application_access_token,
)
from app.core.auth.dependencies import get_auth_settings
from app.core.auth.roles import Role
from app.core.db.dependencies import get_db_session
from app.main import app

ME = "/api/v1/auth/me"
USER_ID = UUID("00000000-0000-0000-0000-0000000000a1")
PARTY_ID = UUID("00000000-0000-0000-0000-0000000000b2")
SESSION_ID = UUID("00000000-0000-0000-0000-0000000000c3")


async def test_me_reports_the_principal_type_the_client_and_the_sorted_scopes():
    async with _client() as client:
        response = await client.get(
            ME, headers=_auth_headers(Scope.CRM_WRITE, Scope.AUTH_CLIENTS_MANAGE, Scope.CRM_READ)
        )

    assert response.status_code == 200
    assert response.json() == {
        "principal_type": "application",
        "client_id": "swagger-operator",
        "scopes": ["auth:clients:manage", "crm:read", "crm:write"],
        "user_id": None,
        "party_id": None,
        "roles": [],
    }


async def test_me_reports_the_account_the_party_and_the_roles_of_a_person():
    async with _client() as client:
        response = await client.get(ME, headers=_person_auth_headers(roles={Role.TUTOR, Role.ADMIN}))

    assert response.status_code == 200
    assert response.json() == {
        "principal_type": "user",
        "client_id": "portal",
        "scopes": ["account:self", "crm:read:own"],
        "user_id": str(USER_ID),
        "party_id": str(PARTY_ID),
        "roles": ["admin", "tutor"],
    }


async def test_me_answers_both_principal_types_without_a_database_session():
    opened: list[object] = []

    async def recording_session() -> AsyncIterator[None]:
        opened.append(object())
        yield None

    async with _client() as client:
        app.dependency_overrides[get_db_session] = recording_session
        application = await client.get(ME, headers=_auth_headers(Scope.AUTH_CLIENTS_MANAGE))
        person = await client.get(ME, headers=_person_auth_headers(roles=set()))

    assert (application.status_code, person.status_code) == (200, 200)
    assert opened == []


async def test_me_accepts_the_person_token_the_broken_ones_are_made_from():
    """Each broken token below changes this one in one claim, so that change is what the 401 answers."""
    async with _client() as client:
        response = await client.get(ME, headers=_hand_signed_person_auth_headers())

    assert response.status_code == 200
    assert response.json()["user_id"] == str(USER_ID)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"without": "party_id"}, id="without-party_id"),
        pytest.param({"without": "sid"}, id="pwd-without-sid"),
        pytest.param({"sid": None}, id="pwd-with-null-sid"),
        pytest.param({"without": "amr"}, id="without-amr"),
        pytest.param({"roles": ["pope"]}, id="unknown-role"),
        pytest.param({"amr": ["otp"]}, id="unknown-method"),
        pytest.param({"amr": ["discord", "pwd"]}, id="mixed-methods"),
        pytest.param({"amr": ["discord"], "scope": "crm:read:own"}, id="discord-with-sid"),
        pytest.param({"amr": ["discord"], "without": "sid"}, id="discord-with-account-self"),
        pytest.param({"sub": "user:someone-else"}, id="sub-not-the-principal"),
    ],
)
async def test_me_with_a_broken_person_token_is_the_401_envelope(changes: dict[str, Any]):
    async with _client() as client:
        response = await client.get(ME, headers=_hand_signed_person_auth_headers(**changes))

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid authentication credentials", "code": "unauthorized"}


async def test_me_accepts_a_discord_token_without_a_session():
    headers = _hand_signed_person_auth_headers(amr=["discord"], scope="crm:read:own", without="sid")

    async with _client() as client:
        response = await client.get(ME, headers=headers)

    assert response.status_code == 200
    assert (response.json()["user_id"], response.json()["scopes"]) == (str(USER_ID), ["crm:read:own"])


async def test_me_without_a_token_is_the_401_envelope():
    async with _client() as client:
        response = await client.get(ME)

    assert response.status_code == 401
    assert ErrorResponse.model_validate(response.json()).code == "unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_me_with_an_invalid_token_is_the_401_envelope():
    async with _client() as client:
        response = await client.get(ME, headers={"Authorization": "Bearer not-a-token"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid authentication credentials", "code": "unauthorized"}


def test_me_is_documented_as_a_guarded_operation_that_needs_no_scope():
    schema = app.openapi()
    operation = schema["paths"][ME]["get"]

    assert operation["operationId"] == "auth_get_me"
    assert [scopes for requirement in operation["security"] for scopes in requirement.values()] == [[]]
    # Any valid token passes, so the derived 401 is documented and a 403 is not.
    assert set(operation["responses"]) == {"200", "401"}

    properties = schema["components"]["schemas"]["MeResponse"]["properties"]
    assert set(properties) == {"principal_type", "client_id", "scopes", "user_id", "party_id", "roles"}
    assert all(prop.get("description") for prop in properties.values())


def test_me_depends_on_the_principal_only():
    """The route answers from the token alone: no session, no settings of its own."""
    assert list(inspect.signature(get_me).parameters) == ["principal"]


@asynccontextmanager
async def _client() -> AsyncIterator[AsyncClient]:
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
        client_id="swagger-operator",
        scopes=[str(scope) for scope in scopes],
    )
    return {"Authorization": f"Bearer {token.access_token}"}


def _person_auth_headers(*, roles: set[Role]) -> dict[str, str]:
    person = UserPrincipal(
        principal_id=USER_ID,
        client_id="portal",
        scopes=frozenset({Scope.ACCOUNT_SELF, Scope.CRM_READ_OWN}),
        party_id=PARTY_ID,
        roles=frozenset(roles),
        login=PasswordLogin(session_id=SESSION_ID),
    )
    token = create_access_token(_auth_settings(), person)
    return {"Authorization": f"Bearer {token.access_token}"}


def _hand_signed_person_auth_headers(*, without: str | None = None, **overrides: object) -> dict[str, str]:
    """A correctly signed person's token with the claims SkillForge writes, but for ``overrides`` and ``without``."""
    settings = _auth_settings()
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": settings.issuer,
        "aud": settings.audience,
        "sub": f"user:{USER_ID}",
        "principal_type": "user",
        "principal_id": str(USER_ID),
        "azp": "portal",
        "scope": "account:self",
        "party_id": str(PARTY_ID),
        "sid": str(SESSION_ID),
        "roles": [],
        "amr": ["pwd"],
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "jti": "00000000-0000-0000-0000-0000000000d4",
    }
    claims |= overrides
    claims.pop(without, None)
    token = jwt.encode(claims, settings.secret_key.get_secret_value(), algorithm=settings.algorithm)
    return {"Authorization": f"Bearer {token}"}


def _auth_settings() -> AuthSettings:
    return AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
