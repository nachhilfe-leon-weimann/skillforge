"""The Discord-user exchange at the service level: the look-up behind it and what it leaves alone (bot-decoupling
spec, P0-7, decision Q)."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthSettings, CreatedAccessToken, Scope
from app.core.auth.roles import Role
from app.core.db.models import UserAccountRoleName
from app.services.auth import TokenDenial, exchange_discord_user
from app.services.auth.accounts import find_user_account_by_discord_user
from app.services.auth.audit import Operator
from app.services.auth.discord_links import link_discord_account, unlink_discord_account
from app.services.auth.roles import stored_roles
from app.services.auth.users import create_user_account
from tests.db.auth.logins import bootstrap_login_client

pytestmark = pytest.mark.db

# Snowflake-sized IDs (> 2**53): the tests fail if an ID passes through a float or a 32-bit integer.
DISCORD_ID = 123456789012345678
OTHER_ID = 987654321098765432
UNKNOWN_ID = 555555555555555555


async def test_an_active_link_finds_the_account_with_its_stored_roles_and_locks_nothing(
    session: AsyncSession, make_person, statements: list[str]
):
    party = await make_person()
    created = await create_user_account(
        session, party_id=party.id, roles=[UserAccountRoleName.ADMIN], actor=Operator.CLI
    )
    await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=party.id, actor=Operator.CLI)
    await session.flush()
    session.expunge_all()  # read as a new request would: nothing loaded yet
    statements.clear()

    account = await find_user_account_by_discord_user(session, DISCORD_ID)

    assert account is not None
    assert account.id == created.account.id
    assert stored_roles(account) == {Role.ADMIN}, "the stored roles are loaded with the account"
    assert statements, "the look-up ran"
    assert not any("FOR UPDATE" in statement for statement in statements), statements


async def test_an_unlinked_or_unknown_discord_user_or_a_party_without_an_account_finds_no_account(
    session: AsyncSession, make_person
):
    party = await make_person()
    await create_user_account(session, party_id=party.id, actor=Operator.CLI)
    await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=party.id, actor=Operator.CLI)
    await unlink_discord_account(session, discord_user_id=DISCORD_ID, actor=Operator.CLI)
    without_account = await make_person("Ben")
    await link_discord_account(session, discord_user_id=OTHER_ID, party_id=without_account.id, actor=Operator.CLI)

    for discord_user_id in (DISCORD_ID, OTHER_ID, UNKNOWN_ID):
        assert await find_user_account_by_discord_user(session, discord_user_id) is None, discord_user_id


async def test_the_exchange_returns_a_bare_access_token_opens_no_session_and_locks_no_row(
    session: AsyncSession, auth_settings: AuthSettings, make_person, statements: list[str]
):
    client = await bootstrap_login_client(session, application=[Scope.AUTH_USERS_EXCHANGE], delegated=[Scope.CRM_READ])
    party = await make_person()
    await create_user_account(session, party_id=party.id, actor=Operator.CLI)
    await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=party.id, actor=Operator.CLI)
    await session.flush()
    statements.clear()

    exchanged = await exchange_discord_user(
        session,
        auth_settings,
        client_id=client.client_id,
        client_secret=client.client_secret,
        discord_user_id=DISCORD_ID,
    )
    denied = await exchange_discord_user(
        session,
        auth_settings,
        client_id=client.client_id,
        client_secret=client.client_secret,
        discord_user_id=UNKNOWN_ID,
    )

    assert isinstance(exchanged, CreatedAccessToken)
    assert exchanged.scope == "crm:read:own"
    assert denied is TokenDenial.INVALID_GRANT
    assert not any("FOR UPDATE" in statement for statement in statements), statements
    assert not any("user_session" in statement for statement in statements), "no session is opened or read"
