"""What two overlapping transactions on one account, or on its party, do to each other.

These need two real transactions, so they cannot run on the rolled-back ``session`` fixture: they commit
and remove what they created - including their audit entries, which do not cascade with the party. The
test's ``session`` only reads what the others committed.
"""

import uuid
from collections.abc import AsyncIterator, Callable, Coroutine

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthSettings, Scope
from app.core.auth.secrets import digest, hash_secret, verify_secret
from app.core.db import Database
from app.core.db.models import (
    ApplicationClient,
    AuthAuditLog,
    Party,
    PermissionScope,
    UserAccount,
    UserActionTokenPurpose,
    UserSession,
)
from app.services.auth import IssuedUserToken, TokenDenial, UserTokenResult, issue_user_token, refresh_user_token
from app.services.auth.action_tokens import issue_action_token, redeem_action_token
from app.services.auth.audit import Operator
from app.services.auth.errors import InvalidActionTokenError, UnknownAccountPartyError
from app.services.auth.results import IssuedActionToken, UserAccountWithRoles
from app.services.auth.users import create_user_account
from app.services.crm import parties, persons
from tests.db.auth.logins import LoginClientCredentials, bootstrap_login_client
from tests.db.auth.overlap import overlapping

pytestmark = pytest.mark.db

FIRST_PASSWORD = "the first password wins"
SECOND_PASSWORD = "the second password loses"


@pytest.fixture
async def party_id(db: Database) -> AsyncIterator[uuid.UUID]:
    """A committed person party, removed afterwards unless a test deleted it."""
    async with db.session() as setup:
        party_id = (await persons.create_person(setup, firstname="Race", lastname="Condition")).id
    try:
        yield party_id
    finally:
        async with db.session() as cleanup:
            if await cleanup.get(Party, party_id) is not None:
                await parties.delete_party(cleanup, party_id)


@pytest.fixture
async def user_id(db: Database, party_id: uuid.UUID) -> AsyncIterator[uuid.UUID]:
    """A committed account with an e-mail address for the party; its audit entries are removed afterwards."""
    async with db.session() as setup:
        view = await create_user_account(setup, party_id=party_id, email="race@example.org", actor=Operator.CLI)
    try:
        yield view.account.id
    finally:
        async with db.session() as cleanup:
            await cleanup.execute(delete(AuthAuditLog).where(AuthAuditLog.principal_id == str(view.account.id)))


async def test_two_issues_arriving_together_leave_one_live_token(
    db: Database, user_id: uuid.UUID, auth_settings: AuthSettings, live_tokens
):
    async def issue(session: AsyncSession) -> IssuedActionToken:
        return await issue_action_token(
            session, auth_settings, user_id=user_id, purpose=UserActionTokenPurpose.INVITATION, actor=Operator.CLI
        )

    await overlapping(db, issue, issue)

    assert len(await live_tokens(user_id)) == 1


async def test_two_overlapping_redeems_of_one_token_set_one_password(
    db: Database, user_id: uuid.UUID, auth_settings: AuthSettings, session: AsyncSession
):
    token = await _invitation(db, user_id, auth_settings)

    def redeem(password: str) -> Callable[[AsyncSession], Coroutine[object, object, UserAccount]]:
        async def _redeem(session: AsyncSession) -> UserAccount:
            return await redeem_action_token(session, plaintext=token, new_password=password, actor=Operator.CLI)

        return _redeem

    second = await overlapping(db, redeem(FIRST_PASSWORD), redeem(SECOND_PASSWORD))

    assert isinstance(second.exception(), InvalidActionTokenError)
    password_hash = await session.scalar(select(UserAccount.password_hash).where(UserAccount.id == user_id))
    assert password_hash is not None
    assert verify_secret(FIRST_PASSWORD, password_hash)
    assert not verify_secret(SECOND_PASSWORD, password_hash)


async def test_a_redeem_racing_the_deletion_of_the_party_is_invalid_action_token(
    db: Database, party_id: uuid.UUID, user_id: uuid.UUID, auth_settings: AuthSettings
):
    token = await _invitation(db, user_id, auth_settings)

    async def delete_the_party(session: AsyncSession) -> None:
        await parties.delete_party(session, party_id)

    async def redeem(session: AsyncSession) -> UserAccount:
        return await redeem_action_token(session, plaintext=token, new_password=FIRST_PASSWORD, actor=Operator.CLI)

    second = await overlapping(db, delete_the_party, redeem)

    assert isinstance(second.exception(), InvalidActionTokenError)


async def test_a_create_racing_the_deletion_of_the_party_is_unknown_account_party(db: Database, party_id: uuid.UUID):
    """The party row is locked before the account is written: the create waits and finds the party gone."""

    async def delete_the_party(session: AsyncSession) -> None:
        await parties.delete_party(session, party_id)

    async def create(session: AsyncSession) -> UserAccountWithRoles:
        return await create_user_account(session, party_id=party_id, actor=Operator.CLI)

    second = await overlapping(db, delete_the_party, create)

    assert isinstance(second.exception(), UnknownAccountPartyError)


async def _invitation(db: Database, user_id: uuid.UUID, settings: AuthSettings) -> str:
    async with db.session() as setup:
        issued = await issue_action_token(
            setup, settings, user_id=user_id, purpose=UserActionTokenPurpose.INVITATION, actor=Operator.CLI
        )
    return issued.plaintext


@pytest.fixture
async def login_client_credentials(db: Database) -> AsyncIterator[LoginClientCredentials]:
    """A committed login client, removed afterwards with its audit entries and the scope rows its grants seeded."""
    async with db.session() as setup:
        known_scopes = set(await setup.scalars(select(PermissionScope.key)))
        credentials = await bootstrap_login_client(setup, client_id="race-portal", delegated=[Scope.ACCOUNT_SELF])
    try:
        yield credentials
    finally:
        async with db.session() as cleanup:
            await cleanup.execute(delete(ApplicationClient).where(ApplicationClient.id == credentials.id))
            await cleanup.execute(delete(AuthAuditLog).where(AuthAuditLog.principal_id == str(credentials.id)))
            await cleanup.execute(delete(PermissionScope).where(PermissionScope.key.not_in(known_scopes)))


async def test_two_overlapping_refreshes_of_one_token_yield_one_rotation_and_leave_the_session_live(
    db: Database, user_id: uuid.UUID, login_client_credentials: LoginClientCredentials, auth_settings: AuthSettings
):
    client_id, client_secret = login_client_credentials.basic
    async with db.session() as setup:
        account = await setup.get_one(UserAccount, user_id)
        account.password_hash = hash_secret(FIRST_PASSWORD)
        await setup.flush()
        login = await issue_user_token(
            setup,
            auth_settings,
            client_id=client_id,
            client_secret=client_secret,
            username="race@example.org",
            password=FIRST_PASSWORD,
        )
    assert isinstance(login, IssuedUserToken)

    first_result: list[UserTokenResult] = []

    async def refresh(session: AsyncSession) -> UserTokenResult:
        return await refresh_user_token(
            session, auth_settings, client_id=client_id, client_secret=client_secret, refresh_token=login.refresh_token
        )

    async def first(session: AsyncSession) -> None:
        first_result.append(await refresh(session))

    second = await overlapping(db, first, refresh)

    [rotated] = first_result
    assert isinstance(rotated, IssuedUserToken)
    assert second.result() is TokenDenial.INVALID_GRANT
    async with db.session() as check:
        user_session = await check.scalar(select(UserSession).where(UserSession.user_account_id == user_id))
    assert user_session is not None
    assert user_session.revoked_at is None
    assert user_session.refresh_token_hash == digest(rotated.refresh_token)
    assert user_session.previous_refresh_token_hash == digest(login.refresh_token)


async def test_two_overlapping_wrong_passwords_on_one_account_both_count(
    db: Database,
    user_id: uuid.UUID,
    login_client_credentials: LoginClientCredentials,
    auth_settings: AuthSettings,
    session: AsyncSession,
):
    client_id, client_secret = login_client_credentials.basic
    async with db.session() as setup:
        (await setup.get_one(UserAccount, user_id)).password_hash = hash_secret(FIRST_PASSWORD)

    async def wrong_password(session: AsyncSession) -> UserTokenResult:
        return await issue_user_token(
            session,
            auth_settings,
            client_id=client_id,
            client_secret=client_secret,
            username="race@example.org",
            password=SECOND_PASSWORD,
        )

    # The first holds the row lock it took to count; the second verifies meanwhile, then waits for it.
    second = await overlapping(db, wrong_password, wrong_password)

    assert second.result() is TokenDenial.INVALID_GRANT
    count = await session.scalar(select(UserAccount.failed_login_count).where(UserAccount.id == user_id))
    assert count == 2
