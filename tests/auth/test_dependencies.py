from typing import Annotated, Any
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI, Security
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.auth import (
    ApplicationPrincipal,
    AuthSettings,
    PasswordLogin,
    Principal,
    Scope,
    UserPrincipal,
    create_access_token,
    create_application_access_token,
    require_application,
    require_scopes,
)
from app.core.auth.dependencies import get_auth_settings, get_current_principal

ManageUsersPrincipal = Annotated[Principal, require_scopes("auth:users:manage")]
# require_scopes refuses a reach-qualified scope, so this requirement is declared with the bare marker.
CrmReadOwnPrincipal = Annotated[Principal, Security(get_current_principal, scopes=["crm:read:own"])]
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
ApplicationOnlyPrincipal = Annotated[ApplicationPrincipal, Depends(require_application)]


async def test_get_current_principal_returns_principal_for_valid_token():
    settings = _settings()
    token = create_application_access_token(
        settings,
        principal_id=uuid4(),
        client_id="integration",
        scopes=["auth:clients:manage"],
    )
    app = _app(settings)

    response = await _request(
        app,
        "GET",
        "/me",
        headers={"Authorization": f"Bearer {token.access_token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "principal_type": "application",
        "client_id": "integration",
        "scopes": ["auth:clients:manage"],
    }


async def test_get_current_principal_rejects_missing_token():
    response = await _request(_app(_settings()), "GET", "/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_get_current_principal_rejects_invalid_token():
    response = await _request(_app(_settings()), "GET", "/me", headers={"Authorization": "Bearer not-a-token"})

    assert response.status_code == 401


async def test_require_scopes_accepts_token_with_required_scope():
    settings = _settings()
    token = create_application_access_token(
        settings,
        principal_id=uuid4(),
        client_id="integration",
        scopes=["auth:users:manage"],
    )
    app = _app(settings)

    response = await _request(
        app,
        "POST",
        "/write",
        headers={"Authorization": f"Bearer {token.access_token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"client_id": "integration"}


async def test_require_scopes_rejects_token_without_required_scope():
    settings = _settings()
    token = create_application_access_token(
        settings,
        principal_id=uuid4(),
        client_id="integration",
        scopes=["auth:clients:manage"],
    )
    app = _app(settings)

    response = await _request(
        app,
        "POST",
        "/write",
        headers={"Authorization": f"Bearer {token.access_token}"},
    )

    assert response.status_code == 403


async def test_require_scopes_guards_a_route_from_the_decorator():
    settings = _settings()
    app = _app(settings)

    missing_token = await _request(app, "POST", "/guarded")
    wrong_scope = await _request(app, "POST", "/guarded", headers=_bearer(settings, scopes=["auth:clients:manage"]))
    granted = await _request(app, "POST", "/guarded", headers=_bearer(settings, scopes=["auth:users:manage"]))

    assert missing_token.status_code == 401
    assert missing_token.headers["www-authenticate"] == 'Bearer scope="auth:users:manage"'
    assert wrong_scope.status_code == 403
    assert granted.status_code == 200


def test_require_scopes_declares_the_scopes_in_openapi_for_both_positions():
    paths = _app(_settings()).openapi()["paths"]

    assert paths["/write"]["post"]["security"] == [{"OAuth2": ["auth:users:manage"]}]
    assert paths["/guarded"]["post"]["security"] == [{"OAuth2": ["auth:users:manage"]}]


async def test_a_crm_read_own_token_is_forbidden_on_a_route_that_requires_crm_read():
    settings = _settings()

    response = await _request(_app(settings), "GET", "/crm", headers=_bearer(settings, scopes=["crm:read:own"]))

    assert response.status_code == 403


async def test_a_crm_read_token_satisfies_a_requirement_of_crm_read_own():
    settings = _settings()

    response = await _request(_app(settings), "GET", "/crm/own", headers=_bearer(settings, scopes=["crm:read"]))

    assert response.status_code == 200
    assert response.json() == {"scopes": ["crm:read"]}


@pytest.mark.parametrize("scope", [Scope.CRM_READ_OWN, "crm:read:own"])
def test_require_scopes_refuses_a_reach_qualified_scope(scope: Scope | str):
    with pytest.raises(ValueError, match="reach-qualified scope crm:read:own"):
        require_scopes(Scope.CRM_READ, scope)


async def test_require_application_rejects_non_application_principal():
    app = FastAPI()

    async def fake_principal() -> Principal:
        return _person(scopes={"auth:clients:manage"})

    app.dependency_overrides[get_current_principal] = fake_principal

    @app.get("/application-only")
    async def application_only(principal: ApplicationOnlyPrincipal):
        return {"principal_type": principal.principal_type}

    response = await _request(app, "GET", "/application-only")

    assert response.status_code == 403


async def test_require_application_rejects_a_person_token_whatever_it_carries():
    """A person is not an application client, whatever scopes their token carries."""
    settings = _settings()
    app = FastAPI()
    app.dependency_overrides[get_auth_settings] = lambda: settings

    @app.get("/application-only")
    async def application_only(principal: ApplicationOnlyPrincipal):
        return {"principal_type": principal.principal_type}

    token = create_access_token(settings, _person(scopes={"account:self", "auth:clients:manage", "crm:write"}))
    response = await _request(
        app, "GET", "/application-only", headers={"Authorization": f"Bearer {token.access_token}"}
    )

    assert response.status_code == 403


async def _request(
    app: FastAPI, method: str, path: str, *, raise_app_exceptions: bool = True, **kwargs: Any
) -> httpx.Response:
    transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


def _app(settings: AuthSettings) -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[get_auth_settings] = lambda: settings

    @app.get("/me")
    async def me(principal: CurrentPrincipal):
        return {
            "principal_type": principal.principal_type,
            "client_id": principal.client_id,
            "scopes": sorted(principal.scopes),
        }

    @app.post("/write")
    async def write(principal: ManageUsersPrincipal):
        return {"client_id": principal.client_id}

    @app.post("/guarded", dependencies=[require_scopes("auth:users:manage")])
    async def guarded():
        return {"ok": True}

    @app.get("/crm", dependencies=[require_scopes(Scope.CRM_READ)])
    async def crm():
        return {"ok": True}

    @app.get("/crm/own")
    async def crm_own(principal: CrmReadOwnPrincipal):
        return {"scopes": sorted(principal.scopes)}

    return app


def _person(*, scopes: set[str]) -> UserPrincipal:
    return UserPrincipal(
        principal_id=uuid4(),
        client_id="portal",
        scopes=frozenset(scopes),
        party_id=uuid4(),
        roles=frozenset(),
        login=PasswordLogin(session_id=uuid4()),
    )


def _bearer(settings: AuthSettings, *, scopes: list[str]) -> dict[str, str]:
    token = create_application_access_token(settings, principal_id=uuid4(), client_id="integration", scopes=scopes)
    return {"Authorization": f"Bearer {token.access_token}"}


def _settings() -> AuthSettings:
    return AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
