"""`Access` and `require_access`: what a reach-aware route receives, per token (user-authentication P0-7)."""

from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.auth import (
    Access,
    AuthSettings,
    PasswordLogin,
    ReachBasis,
    Scope,
    UserPrincipal,
    create_access_token,
    create_application_access_token,
    require_access,
)
from app.core.auth.dependencies import get_auth_settings
from app.core.db.dependencies import get_db_session

PARTY_ID = UUID("22222222-2222-2222-2222-222222222222")
CHILD_ID = UUID("55555555-5555-5555-5555-555555555555")
CrmReadAccess = Annotated[Access, require_access(Scope.CRM_READ)]


def test_access_all_allows_every_party_and_names_no_basis():
    access = Access.all()

    assert access.party_ids is None
    assert access.allows(uuid4())
    assert access.basis(PARTY_ID) is None


def test_access_of_a_reach_allows_exactly_that_reach_and_remembers_why():
    access = Access.of({PARTY_ID: ReachBasis.SELF, CHILD_ID: ReachBasis.GUARDIAN})

    assert access.party_ids == {PARTY_ID, CHILD_ID}
    assert access.allows(CHILD_ID)
    assert not access.allows(uuid4())
    assert access.basis(PARTY_ID) is ReachBasis.SELF
    assert access.basis(CHILD_ID) is ReachBasis.GUARDIAN
    assert access.basis(uuid4()) is None


def test_access_of_keeps_no_reference_to_the_mapping_it_was_built_from():
    reach = {PARTY_ID: ReachBasis.SELF}
    access = Access.of(reach)

    reach[CHILD_ID] = ReachBasis.GUARDIAN

    assert not access.allows(CHILD_ID)


def test_require_access_declares_the_reach_qualified_scope_on_its_marker():
    assert require_access(Scope.CRM_READ).scopes == ["crm:read:own"]


@pytest.mark.parametrize("scope", [Scope.CRM_WRITE, Scope.CRM_READ_OWN, Scope.AUTH_CLIENTS_MANAGE])
def test_require_access_refuses_a_scope_without_a_reach_qualified_variant(scope: Scope):
    with pytest.raises(ValueError, match="reach-qualified variant"):
        require_access(scope)


@pytest.mark.parametrize("token", ["application", "person"])
async def test_a_crm_read_token_gets_access_to_everything_without_a_query(monkeypatch: pytest.MonkeyPatch, token: str):
    """A person holding the unqualified scope (an admin) is not restricted to their reach either."""
    monkeypatch.setattr("app.core.auth.dependencies.resolve_reach", _unexpected_reach)
    mint = _application_token if token == "application" else _person_token

    response = await _get(mint(Scope.CRM_READ))

    assert response.status_code == 200
    assert response.json() == {"party_ids": None}


async def test_a_person_restricted_to_own_gets_their_reach(monkeypatch: pytest.MonkeyPatch):
    resolved: list[UUID] = []

    async def resolve_reach(session: object, party_id: UUID) -> dict[UUID, ReachBasis]:
        resolved.append(party_id)
        return {party_id: ReachBasis.SELF, CHILD_ID: ReachBasis.GUARDIAN}

    monkeypatch.setattr("app.core.auth.dependencies.resolve_reach", resolve_reach)

    response = await _get(_person_token(Scope.ACCOUNT_SELF, Scope.CRM_READ_OWN))

    assert response.status_code == 200
    assert set(response.json()["party_ids"]) == {str(PARTY_ID), str(CHILD_ID)}
    assert resolved == [PARTY_ID]


async def test_an_application_token_with_only_crm_read_own_gets_the_403_of_a_missing_scope(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("app.core.auth.dependencies.resolve_reach", _unexpected_reach)

    response = await _get(_application_token(Scope.CRM_READ_OWN))

    assert response.status_code == 403
    assert response.json() == {"detail": "Not enough permissions"}


@pytest.mark.parametrize("scope", [Scope.CRM_WRITE, Scope.ACCOUNT_SELF])
async def test_a_token_carrying_neither_form_is_403(scope: Scope):
    assert (await _get(_person_token(scope))).status_code == 403
    assert (await _get(_application_token(scope))).status_code == 403


async def test_a_missing_token_is_asked_for_the_reach_qualified_scope():
    response = await _get(None)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer scope="crm:read:own"'


async def _unexpected_reach(session: object, party_id: UUID) -> dict[UUID, ReachBasis]:
    raise AssertionError("resolve_reach must not run for this token")


async def _get(token: str | None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get("/parties", headers=headers)


def _app() -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[get_auth_settings] = _settings
    app.dependency_overrides[get_db_session] = _no_session

    @app.get("/parties")
    async def parties(access: CrmReadAccess) -> dict[str, Any]:
        return {"party_ids": None if access.party_ids is None else sorted(map(str, access.party_ids))}

    return app


async def _no_session() -> AsyncIterator[object]:
    yield object()


def _application_token(*scopes: Scope) -> str:
    return create_application_access_token(
        _settings(), principal_id=uuid4(), client_id="operator", scopes=[str(scope) for scope in scopes]
    ).access_token


def _person_token(*scopes: Scope) -> str:
    principal = UserPrincipal(
        principal_id=uuid4(),
        client_id="portal",
        scopes=frozenset(str(scope) for scope in scopes),
        party_id=PARTY_ID,
        roles=frozenset(),
        login=PasswordLogin(session_id=uuid4()),
    )
    return create_access_token(_settings(), principal).access_token


def _settings() -> AuthSettings:
    return AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
