"""The Discord-user exchange through the real app against the test database (bot-decoupling spec, P0-7).

A client holding `auth:users:exchange` names the Discord user who sent a command and gets a token of the person that
user is actively linked to - no password, no session, no refresh token, and only `VOUCHED_SCOPES`.
"""

import json
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthSettings, DiscordLogin, PrincipalType, Scope, UserPrincipal, validate_access_token
from app.core.auth.roles import Role, scopes_for
from app.core.db.models import (
    AuthAuditLog,
    PartyRelation,
    PartyRelationType,
    PreferredMeetingTool,
    Student,
    Tutor,
    UserAccount,
    UserAccountRoleName,
    UserAccountStatus,
    UserSession,
)
from app.core.logging import LogFormat, LoggingSettings, LogLevel, configure_logging
from app.services.auth.audit import AuditEventType, Operator
from app.services.auth.discord_links import link_discord_account, unlink_discord_account
from app.services.auth.users import create_user_account
from tests.db.auth.logins import LoginClientCredentials, bootstrap_login_client

pytestmark = pytest.mark.db

DISCORD_USER_GRANT = "urn:skillforge:params:oauth:grant-type:discord-user"
# Snowflake-sized IDs (> 2**53): the tests fail if an ID passes through a float or a 32-bit integer.
DISCORD_ID = 123456789012345678
OTHER_ID = 987654321098765432
EMAIL = "anna.schmidt@example.org"
INVALID_GRANT = {"detail": "Invalid credentials or refresh token", "code": "invalid_grant"}
INVALID_SCOPE = {"detail": "Invalid requested scope", "code": "invalid_scope"}
UNAUTHORIZED_CLIENT = {"detail": "Client may not use this grant", "code": "unauthorized_client"}


@pytest.fixture
def make_client(session: AsyncSession) -> Callable[..., Awaitable[LoginClientCredentials]]:
    """Create a client with a secret; by default with the grants the spec plans for skillbot ("Operating")."""

    async def _make_client(
        *, application: Iterable[Scope] = (Scope.AUTH_USERS_EXCHANGE,), delegated: Iterable[Scope] = (Scope.CRM_READ,)
    ) -> LoginClientCredentials:
        return await bootstrap_login_client(
            session, client_id=f"client-{uuid4().hex[:8]}", application=application, delegated=delegated
        )

    return _make_client


@pytest.fixture
async def bot(make_client) -> LoginClientCredentials:
    """An exchange client as skillbot will be set up: `application` `auth:users:exchange`, `delegated` `crm:read` -
    never `auth:users:manage`, `auth:clients:manage` or `auth:users:login` (spec, "Security rules")."""
    return await make_client()


@pytest.fixture
def make_account(session: AsyncSession, make_person) -> Callable[..., Awaitable[UserAccount]]:
    """Create an account of a new person party - no e-mail, no password - and link the Discord users to it."""

    async def _make_account(*discord_user_ids: int, roles: Iterable[UserAccountRoleName] = ()) -> UserAccount:
        party = await make_person()
        view = await create_user_account(session, party_id=party.id, roles=roles, actor=Operator.CLI)
        for discord_user_id in discord_user_ids:
            await _link(session, party.id, discord_user_id)
        return view.account

    return _make_account


@pytest.fixture
def restore_logging():
    """Put the logging configuration back, so the turned-up level ends with the test."""
    yield
    configure_logging(LoggingSettings())


async def _link(session: AsyncSession, party_id: UUID, discord_user_id: int = DISCORD_ID) -> None:
    await link_discord_account(session, discord_user_id=discord_user_id, party_id=party_id, actor=Operator.CLI)


async def _exchange(
    token_api: AsyncClient, client: LoginClientCredentials, discord_user_id: int = DISCORD_ID, **form: str
) -> httpx.Response:
    return await token_api.post(
        "/token",
        auth=client.basic,
        data={"grant_type": DISCORD_USER_GRANT, "discord_user_id": str(discord_user_id), **form},
    )


def _claims(token: str, settings: AuthSettings) -> dict[str, object]:
    return jwt.decode(
        token, settings.secret_key.get_secret_value(), algorithms=[settings.algorithm], audience=settings.audience
    )


def _person(settings: AuthSettings, body: dict) -> UserPrincipal:
    principal = validate_access_token(body["access_token"], settings)
    assert isinstance(principal, UserPrincipal)
    return principal


async def _account_holding(role: Role, make_account, make_person, session: AsyncSession) -> UserAccount:
    """An account linked to ``DISCORD_ID`` whose one role is ``role``: stored (admin) or derived from the CRM."""
    account = await make_account(DISCORD_ID, roles=[UserAccountRoleName.ADMIN] if role is Role.ADMIN else [])
    party_id = account.party_id
    match role:
        case Role.STUDENT:
            session.add(Student(person_id=party_id, preferred_meeting_tool=PreferredMeetingTool.DISCORD))
        case Role.TUTOR:
            session.add(Tutor(person_id=party_id))
        case Role.GUARDIAN:
            child = await make_person("Kim")
            session.add(PartyRelation(from_party_id=party_id, to_party_id=child.id, type=PartyRelationType.PARENT_OF))
        case Role.ADMIN:
            pass
    await session.flush()
    return account


# --- The exchange --------------------------------------------------------------------------------


async def test_an_active_link_to_an_active_account_answers_a_token_without_a_session(
    token_api: AsyncClient,
    bot: LoginClientCredentials,
    make_login_account,
    session: AsyncSession,
    auth_settings: AuthSettings,
    audit_rows,
):
    account = await make_login_account()
    await _link(session, account.party_id)

    response = await _exchange(token_api, bot)
    body = response.json()
    me = await token_api.get("/me", headers={"Authorization": f"Bearer {body['access_token']}"})

    assert response.status_code == 200, response.text
    assert set(body) == {"access_token", "token_type", "expires_in", "scope"}
    assert (body["token_type"], body["expires_in"], body["scope"]) == ("bearer", 900, "crm:read:own")
    claims = _claims(body["access_token"], auth_settings)
    assert claims["amr"] == ["discord"]
    assert "sid" not in claims
    person = _person(auth_settings, body)
    assert (person.principal_id, person.party_id, person.client_id) == (account.id, account.party_id, bot.client_id)
    assert person.login == DiscordLogin()
    assert await session.scalar(select(UserSession).where(UserSession.user_account_id == account.id)) is None
    assert me.status_code == 200
    assert me.json() == {
        "principal_type": "user",
        "client_id": bot.client_id,
        "scopes": ["crm:read:own"],
        "user_id": str(account.id),
        "party_id": str(account.party_id),
        "roles": [],
    }
    issued = await audit_rows(AuditEventType.TOKEN_ISSUED, PrincipalType.USER)
    assert [(row.principal_id, row.detail) for row in issued] == [
        (str(account.id), f"Exchanged Discord user {DISCORD_ID} through client {bot.client_id}.")
    ]


async def test_an_account_without_an_email_or_a_password_exchanges(
    token_api: AsyncClient, bot: LoginClientCredentials, make_account, auth_settings: AuthSettings
):
    account = await make_account(DISCORD_ID)

    response = await _exchange(token_api, bot)

    assert (account.email, account.password_hash) == (None, None)
    assert response.status_code == 200, response.text
    assert _person(auth_settings, response.json()).principal_id == account.id


async def test_two_active_links_of_one_person_both_exchange_for_that_person(
    token_api: AsyncClient, bot: LoginClientCredentials, make_account, auth_settings: AuthSettings
):
    account = await make_account(DISCORD_ID, OTHER_ID)

    responses = [await _exchange(token_api, bot, discord_user_id) for discord_user_id in (DISCORD_ID, OTHER_ID)]

    assert [r.status_code for r in responses] == [200, 200]
    assert {_person(auth_settings, r.json()).principal_id for r in responses} == {account.id}


async def test_the_exchange_accepts_basic_and_form_client_authentication(
    token_api: AsyncClient, bot: LoginClientCredentials, make_account
):
    await make_account(DISCORD_ID)
    form = {"grant_type": DISCORD_USER_GRANT, "discord_user_id": str(DISCORD_ID)}

    basic = await token_api.post("/token", auth=bot.basic, data=form)
    in_form = await token_api.post("/token", data=form | bot.form)
    wrong_secret = await token_api.post("/token", data=form | bot.form | {"client_secret": "wrong"})

    assert [r.status_code for r in (basic, in_form, wrong_secret)] == [200, 200, 401]
    assert wrong_secret.json()["code"] == "invalid_client"


# --- Broken links --------------------------------------------------------------------------------


async def test_every_broken_link_answers_the_same_invalid_grant_and_the_audit_names_the_discord_user(
    token_api: AsyncClient,
    bot: LoginClientCredentials,
    make_account,
    make_person,
    session: AsyncSession,
    audit_rows,
):
    """Unknown, unlinked, linked to a party without an account, linked to a disabled account: `active_link_party_id`
    and the account's status decide, and the bot learns only "no identity" (decision Q)."""
    unknown, unlinked, no_account, disabled = (DISCORD_ID + offset for offset in range(1, 5))
    await make_account(unlinked)
    await unlink_discord_account(session, discord_user_id=unlinked, actor=Operator.CLI)
    await _link(session, (await make_person("Ben")).id, no_account)
    disabled_account = await make_account(disabled)
    disabled_account.status = UserAccountStatus.DISABLED
    await session.flush()

    responses = [await _exchange(token_api, bot, user_id) for user_id in (unknown, unlinked, no_account, disabled)]

    assert {r.status_code for r in responses} == {400}
    assert len({r.content for r in responses}) == 1
    assert responses[0].json() == INVALID_GRANT
    denials = await audit_rows(AuditEventType.TOKEN_DENIED, PrincipalType.USER)
    assert len(denials) == 4
    assert {(row.principal_id, row.detail) for row in denials} == {
        (None, f"Discord user {unknown}: no linked account"),
        (None, f"Discord user {unlinked}: no linked account"),
        (None, f"Discord user {no_account}: no linked account"),
        (str(disabled_account.id), f"Discord user {disabled}: account disabled"),
    }


async def test_a_locked_account_exchanges_and_no_login_field_changes(
    token_api: AsyncClient, bot: LoginClientCredentials, make_login_account, session: AsyncSession
):
    """The lockout guards password guessing, which does not happen here (decision Q)."""
    account = await make_login_account()
    locked_until, last_login_at = datetime.now(UTC) + timedelta(minutes=5), datetime(2026, 1, 1, tzinfo=UTC)
    account.locked_until, account.failed_login_count, account.last_login_at = locked_until, 7, last_login_at
    await session.flush()
    await _link(session, account.party_id)

    response = await _exchange(token_api, bot)

    await session.refresh(account)
    assert response.status_code == 200, response.text
    assert (account.locked_until, account.failed_login_count, account.last_login_at) == (locked_until, 7, last_login_at)


# --- Scopes --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "delegated", "expected"),
    [
        (Role.STUDENT, [Scope.CRM_READ], "crm:read:own"),
        (Role.TUTOR, [Scope.CRM_READ], "crm:read:own"),
        (Role.GUARDIAN, [Scope.CRM_READ], "crm:read:own"),
        (Role.ADMIN, [Scope.CRM_READ], "crm:read"),
        (Role.STUDENT, [Scope.CRM_READ, Scope.CRM_WRITE], "crm:read:own"),
        (Role.TUTOR, [Scope.CRM_READ, Scope.CRM_WRITE], "crm:read:own"),
        (Role.GUARDIAN, [Scope.CRM_READ, Scope.CRM_WRITE], "crm:read:own"),
        (Role.ADMIN, [Scope.CRM_READ, Scope.CRM_WRITE], "crm:read crm:write"),
    ],
)
async def test_the_token_carries_the_overlap_of_the_delegated_grants_the_roles_and_the_vouched_scopes(
    token_api: AsyncClient,
    make_client,
    make_account,
    make_person,
    session: AsyncSession,
    auth_settings: AuthSettings,
    role: Role,
    delegated: list[Scope],
    expected: str,
):
    exchanger = await make_client(delegated=delegated)
    await _account_holding(role, make_account, make_person, session)

    response = await _exchange(token_api, exchanger)

    assert response.status_code == 200, response.text
    assert response.json()["scope"] == expected
    assert _person(auth_settings, response.json()).roles == {role}


async def test_a_client_delegating_account_or_auth_scopes_by_mistake_still_hands_out_vouched_scopes_only(
    token_api: AsyncClient, make_client, make_account, audit_rows
):
    """`VOUCHED_SCOPES` is a ceiling of its own: account and admin actions keep demanding a password login."""
    overgranted = await make_client(delegated=scopes_for([Role.ADMIN]))
    account = await make_account(DISCORD_ID, roles=[UserAccountRoleName.ADMIN])

    everything = await _exchange(token_api, overgranted)
    asked = [
        await _exchange(token_api, overgranted, scope=scope)
        for scope in ("account:self", "auth:users:manage", "auth:clients:manage", "crm:read account:self")
    ]

    assert everything.json()["scope"] == "crm:read crm:write"
    assert [r.json() for r in asked] == [INVALID_SCOPE] * 4
    denials = await audit_rows(AuditEventType.TOKEN_DENIED, PrincipalType.USER)
    assert [(row.principal_id, row.detail) for row in denials] == [
        (str(account.id), f"Discord user {DISCORD_ID}: Requested scopes exceed the ceiling")
    ] * 4


async def test_a_scope_outside_the_grants_or_an_empty_overlap_is_invalid_scope(
    token_api: AsyncClient, bot: LoginClientCredentials, make_client, make_account, audit_rows
):
    account = await make_account(DISCORD_ID)
    self_only = await make_client(delegated=[Scope.ACCOUNT_SELF])

    not_granted = await _exchange(token_api, bot, scope="crm:write")
    no_overlap = await _exchange(token_api, self_only)

    assert [r.json() for r in (not_granted, no_overlap)] == [INVALID_SCOPE] * 2
    denials = await audit_rows(AuditEventType.TOKEN_DENIED, PrincipalType.USER)
    assert {(row.principal_id, row.detail) for row in denials} == {
        (str(account.id), f"Discord user {DISCORD_ID}: Requested scopes are not granted"),
        (str(account.id), f"Discord user {DISCORD_ID}: Client grants and ceilings have no scope in common"),
    }


# --- Clients -------------------------------------------------------------------------------------


async def test_only_an_exchange_client_exchanges_and_an_exchange_client_cannot_log_in(
    token_api: AsyncClient,
    bot: LoginClientCredentials,
    make_client,
    make_login_account,
    session: AsyncSession,
    audit_rows,
    password: str,
):
    """Decision T keeps the two apart: the portal can never vouch, the bot can never take a password."""
    account = await make_login_account()
    await _link(session, account.party_id)
    login_client = await make_client(application=[Scope.AUTH_USERS_LOGIN])
    reader = await make_client(application=[Scope.CRM_READ])

    refused = [
        await _exchange(token_api, login_client),
        await _exchange(token_api, reader),
        await token_api.post(
            "/token", auth=bot.basic, data={"grant_type": "password", "username": EMAIL, "password": password}
        ),
        await token_api.post(
            "/token", auth=bot.basic, data={"grant_type": "refresh_token", "refresh_token": "sf_rt_x"}
        ),
    ]

    assert [(r.status_code, r.json()) for r in refused] == [(400, UNAUTHORIZED_CLIENT)] * 4
    denials = await audit_rows(AuditEventType.TOKEN_DENIED, PrincipalType.APPLICATION)
    assert Counter((row.principal_id, row.detail) for row in denials) == Counter({
        (str(login_client.id), "Client lacks auth:users:exchange"): 1,
        (str(reader.id), "Client lacks auth:users:exchange"): 1,
        (str(bot.id), "Client lacks auth:users:login"): 2,
    })


# --- What never leaves the process ---------------------------------------------------------------


async def test_neither_the_discord_user_id_nor_the_email_reaches_the_log_a_token_or_an_answer(
    token_api: AsyncClient,
    bot: LoginClientCredentials,
    make_login_account,
    session: AsyncSession,
    auth_settings: AuthSettings,
    restore_logging,
    capsys,
):
    """Security rules of the spec: a Discord user ID appears in audit rows only."""
    account = await make_login_account()
    await _link(session, account.party_id)
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()

    granted = await _exchange(token_api, bot)
    unknown = await _exchange(token_api, bot, OTHER_ID)
    refused = await _exchange(token_api, bot, scope="account:self")
    malformed = await token_api.post(
        "/token", auth=bot.basic, data={"grant_type": DISCORD_USER_GRANT, "discord_user_id": f"{DISCORD_ID}x"}
    )
    me = await token_api.get("/me", headers={"Authorization": f"Bearer {granted.json()['access_token']}"})

    output = capsys.readouterr().out
    responses = (granted, unknown, refused, malformed, me)
    assert [r.status_code for r in responses] == [200, 400, 400, 422, 200]
    assert output.count("http_request_") >= len(responses), "the requests were logged"
    claims = json.dumps(_claims(granted.json()["access_token"], auth_settings))
    for secret in (str(DISCORD_ID), str(OTHER_ID), EMAIL):
        assert secret not in output
        assert secret not in claims
        assert not any(secret in r.text for r in responses)
    details = [row.detail or "" for row in await session.scalars(select(AuthAuditLog))]
    assert any(str(DISCORD_ID) in detail for detail in details), "the audit names the Discord user"
    assert not any(EMAIL in detail for detail in details)
