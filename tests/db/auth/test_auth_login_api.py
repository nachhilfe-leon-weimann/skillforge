"""A person's login through a client: the `password` and `refresh_token` grants and `POST /revoke`, run through the
real app against the test database (user-authentication spec, P0-8)."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthSettings, UserPrincipal, validate_access_token
from app.core.auth.principal import PasswordLogin, PrincipalType
from app.core.auth.scopes import Scope, canonical, format_scopes
from app.core.auth.secrets import digest
from app.core.db.models import AuthAuditLog, UserAccount, UserAccountRoleName, UserAccountStatus, UserSession
from app.core.logging import LogFormat, LoggingSettings, LogLevel, configure_logging
from app.services.auth import users as users_service
from app.services.auth.audit import AuditEventType, Operator
from tests.db.auth.logins import PORTAL_DELEGATED_SCOPES, LoginClientCredentials

pytestmark = pytest.mark.db

EMAIL = "anna.schmidt@example.org"
INVALID_GRANT = {"detail": "Invalid credentials or refresh token", "code": "invalid_grant"}
UNAUTHORIZED_CLIENT = {"detail": "Client may not use this grant", "code": "unauthorized_client"}
INVALID_SCOPE = {"detail": "Invalid requested scope", "code": "invalid_scope"}
ADMIN_SCOPES = format_scopes(canonical(PORTAL_DELEGATED_SCOPES))
"""What an admin gets through the portal: its whole ceiling, in canonical form."""


async def _login(
    token_api: AsyncClient, client: LoginClientCredentials, password: str, *, username: str = EMAIL, **form: str
) -> httpx.Response:
    return await token_api.post(
        "/token", auth=client.basic, data={"grant_type": "password", "username": username, "password": password, **form}
    )


async def _refresh(
    token_api: AsyncClient, client: LoginClientCredentials, refresh_token: str, **form: str
) -> httpx.Response:
    return await token_api.post(
        "/token", auth=client.basic, data={"grant_type": "refresh_token", "refresh_token": refresh_token, **form}
    )


async def _logged_in(token_api: AsyncClient, client: LoginClientCredentials, password: str, **form: str) -> dict:
    response = await _login(token_api, client, password, **form)
    assert response.status_code == 200, response.text
    return response.json()


async def _client_token(token_api: AsyncClient, client: LoginClientCredentials) -> dict[str, str]:
    """The bearer header of ``client``'s own token: redeem and revoke take the client's token."""
    response = await token_api.post("/token", auth=client.basic, data={"grant_type": "client_credentials"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _person(settings: AuthSettings, body: dict) -> UserPrincipal:
    principal = validate_access_token(body["access_token"], settings)
    assert isinstance(principal, UserPrincipal)
    return principal


async def _session_of(session: AsyncSession, refresh_token: str) -> UserSession:
    statement = select(UserSession).where(
        (UserSession.refresh_token_hash == digest(refresh_token))
        | (UserSession.previous_refresh_token_hash == digest(refresh_token))
    )
    user_session = await session.scalar(statement.execution_options(populate_existing=True))
    assert user_session is not None
    return user_session


async def _fresh(session: AsyncSession, account: UserAccount) -> UserAccount:
    await session.refresh(account)
    return account


@pytest.fixture
def restore_logging():
    """Put the logging configuration back, so the turned-up level ends with the test."""
    yield
    configure_logging(LoggingSettings())


# --- The lifecycle -------------------------------------------------------------------------------


async def test_the_login_lifecycle_from_account_creation_to_logout(
    operator: AsyncClient,
    token_api: AsyncClient,
    login_client,
    make_person,
    session: AsyncSession,
    password: str,
    auth_settings: AuthSettings,
    restore_logging,
    capsys,
):
    party = await make_person()
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()

    user_id = (await operator.post("/users", json={"party_id": str(party.id), "email": EMAIL})).json()["id"]
    invitation = (await operator.post(f"/users/{user_id}/invitation")).json()["token"]
    redeemed = await operator.post("/password/redeem", json={"token": invitation, "new_password": password})
    wrong = await _login(token_api, login_client, "not the password")
    unknown = await _login(token_api, login_client, password, username="nobody@example.org")
    login = await _login(token_api, login_client, password)
    me = await token_api.get("/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"})
    refreshed = await _refresh(token_api, login_client, login.json()["refresh_token"])
    revoked = await token_api.post(
        "/revoke",
        json={"refresh_token": refreshed.json()["refresh_token"]},
        headers=await _client_token(token_api, login_client),
    )
    after_logout = await _refresh(token_api, login_client, refreshed.json()["refresh_token"])

    output = capsys.readouterr().out
    assert [r.status_code for r in (redeemed, wrong, unknown, login, me, refreshed, revoked, after_logout)] == [
        204, 400, 400, 200, 200, 200, 204, 400,
    ]  # fmt: skip
    assert after_logout.json() == INVALID_GRANT
    assert me.json() | {"scopes": None} == {
        "principal_type": "user",
        "client_id": login_client.client_id,
        "scopes": None,
        "user_id": user_id,
        "party_id": str(party.id),
        "roles": [],
    }
    assert set(me.json()["scopes"]) == {"account:self", "crm:read:own"}
    user_session = await _session_of(session, refreshed.json()["refresh_token"])
    assert (user_session.revoked_reason, str(user_session.user_account_id)) == ("logout", user_id)

    # Neither the address, the password nor a token leaves the process.
    secrets = [EMAIL, password, invitation, login.json()["refresh_token"], refreshed.json()["refresh_token"]]
    assert output.count("http_request_") >= 8, "the flow logged its requests"
    audit_rows = list(await session.scalars(select(AuthAuditLog)))
    audit_text = " ".join(f"{row.principal_type} {row.principal_id} {row.detail}" for row in audit_rows)
    for secret in [*secrets, "nobody@example.org", "example.org"]:
        assert secret not in output
        assert secret not in audit_text


async def test_a_login_and_a_refresh_open_one_session_and_rotate_it(
    token_api: AsyncClient, login_client, make_login_account, session: AsyncSession, password: str
):
    account = await make_login_account()
    before = datetime.now(UTC)

    login = await _logged_in(token_api, login_client, password)
    user_session = await _session_of(session, login["refresh_token"])
    refreshed = (await _refresh(token_api, login_client, login["refresh_token"])).json()
    await session.refresh(user_session)

    assert set(login) == {"access_token", "token_type", "expires_in", "scope", "refresh_token", "refresh_expires_in"}
    assert login["refresh_token"].startswith("sf_rt_")
    assert 30 * 86400 - 60 <= login["refresh_expires_in"] <= 30 * 86400
    assert refreshed["refresh_expires_in"] <= login["refresh_expires_in"], "a refresh never extends the session"
    assert user_session.user_account_id == account.id
    assert user_session.application_client_id == login_client.id
    assert user_session.scope == "account:self crm:read:own"
    assert user_session.refresh_token_hash == digest(refreshed["refresh_token"])
    assert user_session.previous_refresh_token_hash == digest(login["refresh_token"])
    assert user_session.rotated_at is not None and user_session.last_used_at == user_session.rotated_at
    last_login_at = (await _fresh(session, account)).last_login_at
    assert last_login_at is not None and last_login_at >= before


async def test_both_the_login_and_the_refresh_carry_amr_pwd_and_the_session_id(
    token_api: AsyncClient, login_client, make_login_account, session, password: str, auth_settings: AuthSettings
):
    account = await make_login_account()

    login = await _logged_in(token_api, login_client, password)
    refreshed = (await _refresh(token_api, login_client, login["refresh_token"])).json()

    user_session = await _session_of(session, refreshed["refresh_token"])
    for body in (login, refreshed):
        person = _person(auth_settings, body)
        assert person.login == PasswordLogin(session_id=user_session.id)
        assert (person.principal_id, person.party_id) == (account.id, account.party_id)
        assert person.client_id == login_client.client_id


async def test_person_grants_record_the_account_as_a_user(
    token_api: AsyncClient, login_client, make_login_account, audit_events, password: str
):
    account = await make_login_account()

    login = await _logged_in(token_api, login_client, password)
    await _refresh(token_api, login_client, login["refresh_token"])

    assert (await audit_events(account.id))[AuditEventType.TOKEN_ISSUED] == 2


# --- Client authentication -----------------------------------------------------------------------


async def test_client_credentials_answers_with_exactly_the_four_keys(token_api: AsyncClient, login_client):
    response = await token_api.post("/token", data={"grant_type": "client_credentials", **login_client.form})

    assert response.status_code == 200
    assert set(response.json()) == {"access_token", "token_type", "expires_in", "scope"}


async def test_password_accepts_basic_and_form_client_authentication_and_basic_wins(
    token_api: AsyncClient, login_client, make_login_account, password: str
):
    await make_login_account()
    form = {"grant_type": "password", "username": EMAIL, "password": password}

    basic = await token_api.post("/token", auth=login_client.basic, data=form)
    in_form = await token_api.post("/token", data=form | login_client.form)
    basic_wins = await token_api.post(
        "/token", auth=login_client.basic, data=form | {"client_id": login_client.client_id, "client_secret": "wrong"}
    )
    form_loses = await token_api.post("/token", auth=(login_client.client_id, "wrong"), data=form | login_client.form)

    assert [r.status_code for r in (basic, in_form, basic_wins, form_loses)] == [200, 200, 200, 401]
    assert form_loses.json()["code"] == "invalid_client"


async def test_a_client_without_auth_users_login_is_unauthorized_client_for_both_grants(
    token_api: AsyncClient, login_client, make_login_client, make_login_account, audit_rows, password: str
):
    await make_login_account()
    refresh_token = (await _logged_in(token_api, login_client, password))["refresh_token"]
    outsider = await make_login_client(application=[Scope.CRM_READ])

    login = await _login(token_api, outsider, password)
    refresh = await _refresh(token_api, outsider, refresh_token)

    assert (login.status_code, login.json()) == (400, UNAUTHORIZED_CLIENT)
    assert (refresh.status_code, refresh.json()) == (400, UNAUTHORIZED_CLIENT)
    # Step 2 is the client's own denial: recorded against the client, as `invalid_client` is.
    denials = await audit_rows(AuditEventType.TOKEN_DENIED)
    assert [(row.principal_type, row.principal_id, row.success, row.detail) for row in denials] == [
        (PrincipalType.APPLICATION, str(outsider.id), False, "Client lacks auth:users:login")
    ] * 2


@pytest.mark.parametrize("field", ["client_id", "client_secret"])
async def test_an_over_long_client_credential_is_invalid_client_and_reaches_neither_audit_nor_log(
    token_api: AsyncClient, login_client, audit_rows, field: str, restore_logging, capsys
):
    over_long = "x" * 8000
    form = login_client.form | {field: over_long}
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()

    responses = [
        await token_api.post("/token", data={"grant_type": grant_type, "username": EMAIL, "password": "pw", **form})
        for grant_type in ("client_credentials", "password")
    ]

    assert [(r.status_code, r.json()["code"]) for r in responses] == [(401, "invalid_client")] * 2
    denials = await audit_rows(AuditEventType.TOKEN_DENIED)
    assert [(row.principal_type, row.principal_id) for row in denials] == [(PrincipalType.APPLICATION, None)] * 2
    assert not any(over_long in (row.detail or "") for row in denials)
    output = capsys.readouterr().out
    assert output.count("http_request_") >= 2, "the requests were logged"
    assert over_long not in output


async def test_a_client_credential_of_the_longest_length_is_still_looked_up(token_api: AsyncClient, audit_rows):
    longest = "x" * 255

    response = await token_api.post(
        "/token", data={"grant_type": "client_credentials", "client_id": longest, "client_secret": "secret"}
    )

    assert response.json()["code"] == "invalid_client"
    assert [row.principal_id for row in await audit_rows(AuditEventType.TOKEN_DENIED)] == [longest]


async def test_the_login_scope_granted_as_delegated_does_not_make_a_login_client(
    token_api: AsyncClient, make_login_client, make_login_account, password: str
):
    """`auth:users:login` counts in `application` mode only - and cannot be granted as `delegated` anyway."""
    await make_login_account()
    client = await make_login_client(application=[Scope.CRM_READ], delegated=[Scope.ACCOUNT_SELF])

    response = await _login(token_api, client, password)

    assert response.json() == UNAUTHORIZED_CLIENT


@pytest.mark.parametrize(
    "form",
    [
        {"grant_type": "password", "username": EMAIL},
        {"grant_type": "password", "password": "x"},
        {"grant_type": "refresh_token"},
    ],
)
async def test_a_parameter_the_grant_needs_and_lacks_is_invalid_request(
    token_api: AsyncClient, login_client, form: dict[str, str]
):
    response = await token_api.post("/token", auth=login_client.basic, data=form)

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


# --- Denied logins -------------------------------------------------------------------------------


async def test_every_refused_login_answers_the_identical_invalid_grant_body(
    token_api: AsyncClient, login_client, make_login_account, session, password: str
):
    wrong_password = await make_login_account("wrong@example.org")
    disabled = await make_login_account("disabled@example.org")
    disabled.status = UserAccountStatus.DISABLED
    no_password = await make_login_account("nopassword@example.org")
    no_password.password_hash = None
    locked = await make_login_account("locked@example.org")
    locked.locked_until = datetime.now(UTC) + timedelta(minutes=5)
    await session.flush()

    responses = [
        await _login(token_api, login_client, password, username="unknown@example.org"),
        await _login(token_api, login_client, "not the password", username=wrong_password.email),
        await _login(token_api, login_client, password, username=disabled.email),
        await _login(token_api, login_client, password, username=no_password.email),
        await _login(token_api, login_client, password, username=locked.email),
        await _login(token_api, login_client, password, username="no address at all"),
    ]

    assert {r.status_code for r in responses} == {400}
    assert len({r.content for r in responses}) == 1
    assert responses[0].json() == INVALID_GRANT
    assert (await _fresh(session, locked)).failed_login_count == 0, "a locked account's counter does not move"


@pytest.mark.parametrize(
    "username", ["Anna.Schmidt@Example.org", "  anna.schmidt@example.org  ", "Anna Schmidt <Anna.Schmidt@example.org>"]
)
async def test_every_spelling_of_the_address_logs_in_to_the_account(
    token_api: AsyncClient, login_client, make_login_account, password: str, auth_settings: AuthSettings, username: str
):
    account = await make_login_account()

    login = await _logged_in(token_api, login_client, password, username=username)

    assert _person(auth_settings, login).principal_id == account.id


async def test_a_denied_login_names_the_account_it_matched_and_none_otherwise(
    token_api: AsyncClient, login_client, make_login_account, audit_rows, password: str
):
    account = await make_login_account()

    await _login(token_api, login_client, "not the password")
    await _login(token_api, login_client, password, username="unknown@example.org")

    denials = await audit_rows(AuditEventType.TOKEN_DENIED, PrincipalType.USER)
    assert sorted((row.principal_id or "", row.success) for row in denials) == [("", False), (str(account.id), False)]
    assert not any("example.org" in (row.detail or "") for row in denials)


async def test_after_five_wrong_passwords_the_right_one_is_refused_and_the_counter_is_persisted(
    token_api: AsyncClient, login_client, make_login_account, session: AsyncSession, password: str
):
    account = await make_login_account()

    for _ in range(5):
        assert (await _login(token_api, login_client, "not the password")).json() == INVALID_GRANT
    refused = await _login(token_api, login_client, password)

    # Every request ran in its own SAVEPOINT that a raised error rolls back: what the denials wrote survived.
    account = await _fresh(session, account)
    assert refused.json() == INVALID_GRANT
    assert account.failed_login_count == 5
    assert account.locked_until is not None
    assert timedelta(seconds=50) < account.locked_until - datetime.now(UTC) <= timedelta(minutes=1)

    account.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    await session.flush()
    assert (await _login(token_api, login_client, password)).status_code == 200
    account = await _fresh(session, account)
    assert (account.failed_login_count, account.locked_until) == (0, None)


async def test_a_denied_scope_keeps_the_counter_reset_and_opens_no_session(
    token_api: AsyncClient, login_client, make_login_account, session: AsyncSession, audit_rows, password: str
):
    account = await make_login_account()
    account.failed_login_count = 3
    await session.flush()

    response = await _login(token_api, login_client, password, scope="crm:write")

    assert response.json() == INVALID_SCOPE
    assert (await _fresh(session, account)).failed_login_count == 0
    assert await session.scalar(select(UserSession).where(UserSession.user_account_id == account.id)) is None
    denials = await audit_rows(AuditEventType.TOKEN_DENIED, PrincipalType.USER)
    assert [row.principal_id for row in denials] == [str(account.id)]


# --- Scopes --------------------------------------------------------------------------------------


async def test_an_admin_through_a_client_delegating_only_account_self_gets_exactly_account_self(
    token_api: AsyncClient, make_login_client, make_login_account, password: str
):
    await make_login_account(roles=[UserAccountRoleName.ADMIN])
    client = await make_login_client(delegated=[Scope.ACCOUNT_SELF])

    login = await _logged_in(token_api, client, password)

    assert login["scope"] == "account:self"


async def test_an_admin_gets_the_admin_scopes_and_loses_them_on_refresh_once_the_role_is_gone(
    token_api: AsyncClient, login_client, make_login_account, session: AsyncSession, password: str
):
    account = await make_login_account(roles=[UserAccountRoleName.ADMIN])
    login = await _logged_in(token_api, login_client, password)

    await users_service.remove_user_role(session, account.id, role=UserAccountRoleName.ADMIN, actor=Operator.CLI)
    refreshed = (await _refresh(token_api, login_client, login["refresh_token"])).json()

    assert login["scope"] == ADMIN_SCOPES
    assert refreshed["scope"] == "account:self crm:read:own"


async def test_a_refresh_narrows_with_scope_and_returns_to_the_session_scope_without_it(
    token_api: AsyncClient, login_client, make_login_account, password: str
):
    await make_login_account(roles=[UserAccountRoleName.ADMIN])
    login = await _logged_in(token_api, login_client, password)

    narrowed = (
        await _refresh(token_api, login_client, login["refresh_token"], scope="account:self crm:read:own")
    ).json()
    full = (await _refresh(token_api, login_client, narrowed["refresh_token"])).json()

    assert narrowed["scope"] == "account:self crm:read:own"
    assert full["scope"] == ADMIN_SCOPES


async def test_a_refresh_asking_for_more_than_the_session_scope_is_invalid_scope_and_rotates_nothing(
    token_api: AsyncClient, login_client, make_login_account, session: AsyncSession, password: str
):
    await make_login_account(roles=[UserAccountRoleName.ADMIN])
    login = await _logged_in(token_api, login_client, password, scope="account:self crm:read:own")

    wider = await _refresh(token_api, login_client, login["refresh_token"], scope="crm:write")
    unnarrowed = await _refresh(token_api, login_client, login["refresh_token"])

    assert login["scope"] == "account:self crm:read:own"
    assert wider.json() == INVALID_SCOPE
    assert unnarrowed.json()["scope"] == "account:self crm:read:own", "the session scope is the ceiling"


# --- Refresh -------------------------------------------------------------------------------------


async def test_a_refresh_token_is_refused_for_another_client_and_the_session_is_left_alone(
    token_api: AsyncClient, login_client, make_login_client, make_login_account, session: AsyncSession, password: str
):
    await make_login_account()
    login = await _logged_in(token_api, login_client, password)
    other = await make_login_client()

    stolen = await _refresh(token_api, other, login["refresh_token"])
    revoked = await token_api.post(
        "/revoke", json={"refresh_token": login["refresh_token"]}, headers=await _client_token(token_api, other)
    )
    own = await _refresh(token_api, login_client, login["refresh_token"])

    assert stolen.json() == INVALID_GRANT
    assert revoked.status_code == 204
    assert own.status_code == 200


async def test_a_refresh_of_a_disabled_account_revokes_the_session(
    token_api: AsyncClient, login_client, make_login_account, session: AsyncSession, audit_events, password: str
):
    account = await make_login_account()
    login = await _logged_in(token_api, login_client, password)
    account.status = UserAccountStatus.DISABLED
    await session.flush()

    response = await _refresh(token_api, login_client, login["refresh_token"])

    assert response.json() == INVALID_GRANT
    assert (await _session_of(session, login["refresh_token"])).revoked_reason == "account_disabled"
    assert (await audit_events(account.id))[AuditEventType.SESSION_REVOKED] == 1


async def test_an_expired_or_unknown_refresh_token_is_invalid_grant(
    token_api: AsyncClient, login_client, make_login_account, session: AsyncSession, password: str
):
    await make_login_account()
    login = await _logged_in(token_api, login_client, password)
    user_session = await _session_of(session, login["refresh_token"])
    user_session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.flush()

    expired = await _refresh(token_api, login_client, login["refresh_token"])
    unknown = await _refresh(token_api, login_client, "sf_rt_unknown")

    assert expired.json() == unknown.json() == INVALID_GRANT


# --- Revoke --------------------------------------------------------------------------------------


async def test_revoke_answers_204_for_an_unknown_token(token_api: AsyncClient, login_client):
    response = await token_api.post(
        "/revoke", json={"refresh_token": "sf_rt_unknown"}, headers=await _client_token(token_api, login_client)
    )

    assert response.status_code == 204


async def test_revoke_with_a_rotated_out_token_ends_the_session(
    token_api: AsyncClient, login_client, make_login_account, session: AsyncSession, password: str
):
    await make_login_account()
    login = await _logged_in(token_api, login_client, password)
    refreshed = (await _refresh(token_api, login_client, login["refresh_token"])).json()

    await token_api.post(
        "/revoke", json={"refresh_token": login["refresh_token"]}, headers=await _client_token(token_api, login_client)
    )

    assert (await _session_of(session, refreshed["refresh_token"])).revoked_reason == "logout"
    assert (await _refresh(token_api, login_client, refreshed["refresh_token"])).json() == INVALID_GRANT


async def test_revoke_refuses_a_persons_token_and_a_client_without_auth_users_login(
    token_api: AsyncClient, login_client, make_login_client, make_login_account, password: str
):
    await make_login_account(roles=[UserAccountRoleName.ADMIN])
    login = await _logged_in(token_api, login_client, password)
    outsider = await make_login_client(application=[Scope.CRM_READ])
    body = {"refresh_token": login["refresh_token"]}

    as_person = await token_api.post("/revoke", json=body, headers={"Authorization": f"Bearer {login['access_token']}"})
    as_outsider = await token_api.post("/revoke", json=body, headers=await _client_token(token_api, outsider))

    assert (as_person.status_code, as_outsider.status_code) == (403, 403)
    assert (await _refresh(token_api, login_client, login["refresh_token"])).status_code == 200


async def test_a_session_the_login_opened_is_one_the_admin_route_revokes(
    token_api: AsyncClient, operator: AsyncClient, login_client, make_login_account, password: str
):
    account = await make_login_account()
    login = await _logged_in(token_api, login_client, password)

    await operator.delete(f"/users/{account.id}/sessions")

    assert (await _refresh(token_api, login_client, login["refresh_token"])).json() == INVALID_GRANT
