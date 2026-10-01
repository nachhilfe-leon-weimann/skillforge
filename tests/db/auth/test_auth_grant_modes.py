"""Client grants have a mode (ADR 0008): `application` for the client itself, `delegated` as the ceiling for people."""

from collections import Counter

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import GrantMode
from app.services.auth import (
    ApplicationClientScopeGrantNotFoundError,
    InvalidClientScopeError,
    create_application_client,
    create_application_client_secret,
    grant_application_client_scopes,
    issue_client_token,
    revoke_application_client_scope,
)

APPLICATION, DELEGATED = GrantMode.APPLICATION, GrantMode.DELEGATED


@pytest.mark.db
async def test_one_scope_is_granted_and_revoked_in_each_mode_on_its_own(session, grants):
    await create_application_client(session, client_id="portal", name="Portal")

    await grant_application_client_scopes(session, client_id="portal", scopes=["crm:read"], mode=DELEGATED)
    client = await grant_application_client_scopes(session, client_id="portal", scopes=["crm:read"], mode=APPLICATION)
    granted = {(grant.scope_key, grant.mode) for grant in client.scope_grants}
    await revoke_application_client_scope(session, client_id="portal", scope_key="crm:read", mode=DELEGATED)

    assert granted == {("crm:read", APPLICATION), ("crm:read", DELEGATED)}
    assert await grants() == {("crm:read", APPLICATION)}
    with pytest.raises(ApplicationClientScopeGrantNotFoundError):
        await revoke_application_client_scope(session, client_id="portal", scope_key="crm:read", mode=DELEGATED)


@pytest.mark.db
async def test_a_scope_granted_again_in_its_mode_is_kept_as_it_is(session, grants, scope_grant_details):
    await create_application_client(session, client_id="portal", name="Portal")

    await grant_application_client_scopes(session, client_id="portal", scopes=["crm:read"], mode=DELEGATED)
    await grant_application_client_scopes(session, client_id="portal", scopes=["crm:read", "crm:write"], mode=DELEGATED)

    assert await grants() == {("crm:read", DELEGATED), ("crm:write", DELEGATED)}
    assert await scope_grant_details() == Counter([
        "Granted scope crm:read in delegated mode to application client portal.",
        "Granted scope crm:write in delegated mode to application client portal.",
    ])


@pytest.mark.db
async def test_the_audit_entries_name_the_mode(session, scope_grant_details):
    await create_application_client(session, client_id="portal", name="Portal")

    await grant_application_client_scopes(session, client_id="portal", scopes=["auth:users:login"])
    await grant_application_client_scopes(session, client_id="portal", scopes=["account:self"], mode=DELEGATED)
    await revoke_application_client_scope(session, client_id="portal", scope_key="account:self", mode=DELEGATED)
    await revoke_application_client_scope(session, client_id="portal", scope_key="auth:users:login")

    assert await scope_grant_details() == Counter([
        "Granted scope auth:users:login in application mode to application client portal.",
        "Granted scope account:self in delegated mode to application client portal.",
        "Removed scope account:self in delegated mode from application client portal.",
        "Removed scope auth:users:login in application mode from application client portal.",
    ])


@pytest.mark.db
async def test_a_client_only_scope_is_refused_as_delegated_and_nothing_of_the_request_is_granted(
    session, grants, scope_grant_details
):
    await create_application_client(session, client_id="portal", name="Portal")

    # `account:self` sorts before `auth:users:login`: the whole request is checked before anything is granted.
    with pytest.raises(InvalidClientScopeError):
        await grant_application_client_scopes(
            session, client_id="portal", scopes=["account:self", "auth:users:login"], mode=DELEGATED
        )

    assert await grants() == set()
    assert await scope_grant_details() == Counter()

    await grant_application_client_scopes(session, client_id="portal", scopes=["auth:users:login"], mode=APPLICATION)
    assert await grants() == {("auth:users:login", APPLICATION)}


@pytest.mark.db
async def test_an_unknown_scope_refuses_the_whole_request(session, grants):
    await create_application_client(session, client_id="portal", name="Portal")

    with pytest.raises(InvalidClientScopeError):
        await grant_application_client_scopes(session, client_id="portal", scopes=["auth:clients:manage", "nope:scope"])

    assert await grants() == set()


@pytest.mark.db
async def test_client_credentials_draws_on_application_grants_only(session, auth_settings):
    secret = await _client_with_secret(session)
    await grant_application_client_scopes(session, client_id="portal", scopes=["auth:clients:manage"], mode=APPLICATION)
    await grant_application_client_scopes(session, client_id="portal", scopes=["crm:read"], mode=DELEGATED)

    token = await issue_client_token(session, auth_settings, client_id="portal", client_secret=secret)

    assert token.scope == "auth:clients:manage"
    for requested in ("crm:read", "crm:read:own"):
        with pytest.raises(InvalidClientScopeError):
            await issue_client_token(
                session, auth_settings, client_id="portal", client_secret=secret, requested_scopes=requested
            )


@pytest.mark.db
async def test_a_client_with_delegated_grants_only_gets_no_client_credentials_token(session, auth_settings):
    secret = await _client_with_secret(session)
    await grant_application_client_scopes(session, client_id="portal", scopes=["crm:read"], mode=DELEGATED)

    with pytest.raises(InvalidClientScopeError):
        await issue_client_token(session, auth_settings, client_id="portal", client_secret=secret)


async def _client_with_secret(session: AsyncSession) -> str:
    await create_application_client(session, client_id="portal", name="Portal")
    return (await create_application_client_secret(session, client_id="portal")).plaintext
