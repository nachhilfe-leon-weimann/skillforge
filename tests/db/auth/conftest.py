"""Fixtures for auth tests that run the real app, or the auth services, against the test database."""

from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import bootstrap
from app.core.auth import AuthSettings, PrincipalType, Scope, create_application_access_token
from app.core.auth.dependencies import get_auth_settings
from app.core.auth.secrets import hash_secret
from app.core.db.dependencies import get_db_session
from app.core.db.models import (
    ApplicationClient,
    ApplicationClientScopeGrant,
    ApplicationClientStatus,
    AuthAuditLog,
    Company,
    GrantMode,
    Party,
    PartyType,
    Person,
    UserAccount,
    UserAccountRoleName,
    UserActionToken,
    UserSession,
)
from app.main import app
from app.services.auth.audit import AuditEventType, Operator
from app.services.auth.users import create_user_account
from tests.db.auth.logins import PORTAL_DELEGATED_SCOPES, LoginClientCredentials, bootstrap_login_client

AUTH_SETTINGS = AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
OPERATOR_ID = UUID("00000000-0000-0000-0000-000000000001")
FAR_FUTURE = datetime(2099, 1, 1, tzinfo=UTC)


def auth_headers(*scopes: Scope) -> dict[str, str]:
    """A bearer token of an application client holding ``scopes``."""
    token = create_application_access_token(
        AUTH_SETTINGS,
        principal_id=OPERATOR_ID,
        client_id="operator",
        scopes=[str(scope) for scope in scopes],
    )
    return {"Authorization": f"Bearer {token.access_token}"}


@pytest.fixture
def auth_settings() -> AuthSettings:
    """The settings the ``client`` fixture signs and verifies its tokens with."""
    return AUTH_SETTINGS


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """An API client holding `auth:clients:manage` whose requests run on the test's ``session``."""
    async with _api_client(session, Scope.AUTH_CLIENTS_MANAGE) as api_client:
        yield api_client


@pytest.fixture
async def operator(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """An API client holding `auth:users:manage` and `auth:users:login` whose requests run on the test's ``session``.

    The operator of "Operating without a portal": it manages accounts and redeems their tokens.
    """
    async with _api_client(session, Scope.AUTH_USERS_MANAGE, Scope.AUTH_USERS_LOGIN) as api_client:
        yield api_client


@pytest.fixture
async def link_admin(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """An API client holding `auth:users:manage` and `auth:discord-links:read`: it links, unlinks and reads links."""
    async with _api_client(session, Scope.AUTH_USERS_MANAGE, Scope.AUTH_DISCORD_LINKS_READ) as api_client:
        yield api_client


@asynccontextmanager
async def _api_client(session: AsyncSession, *scopes: Scope) -> AsyncIterator[AsyncClient]:
    """An API client holding ``scopes`` whose requests run on ``session``.

    Every request is wrapped in a SAVEPOINT that is rolled back when the request fails, which
    mirrors the request-scoped transaction of ``get_db_session``: a failed request writes nothing.
    """

    async with _app_client(session, headers=auth_headers(*scopes)) as api_client:
        yield api_client


@asynccontextmanager
async def _app_client(session: AsyncSession, *, headers: dict[str, str] | None = None) -> AsyncIterator[AsyncClient]:
    """An API client of the auth routes whose requests run on ``session``, each in a SAVEPOINT (see ``_api_client``)."""

    async def request_session() -> AsyncIterator[AsyncSession]:
        async with session.begin_nested():
            yield session

    app.dependency_overrides[get_db_session] = request_session
    app.dependency_overrides[get_auth_settings] = lambda: AUTH_SETTINGS
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver/api/v1/auth",
            headers=headers,
        ) as api_client:
            yield api_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def token_api(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """An API client without a token of its own, for `POST /token`: every grant authenticates the client itself."""
    async with _app_client(session) as api_client:
        yield api_client


@pytest.fixture
def make_login_client(session: AsyncSession) -> Callable[..., Awaitable[LoginClientCredentials]]:
    """Create a client with a secret on the test's ``session`` (see ``bootstrap_login_client``)."""

    async def _make_login_client(
        *,
        application: Iterable[Scope] = (Scope.AUTH_USERS_LOGIN,),
        delegated: Iterable[Scope] = PORTAL_DELEGATED_SCOPES,
    ) -> LoginClientCredentials:
        return await bootstrap_login_client(session, application=application, delegated=delegated)

    return _make_login_client


@pytest.fixture
async def login_client(make_login_client) -> LoginClientCredentials:
    """A login client with the portal's ceiling."""
    return await make_login_client()


@pytest.fixture
def make_login_account(session: AsyncSession, make_person, password: str) -> Callable[..., Awaitable[UserAccount]]:
    """Create an account of a new person party that logs in with ``email`` and the ``password`` fixture."""

    async def _make_login_account(
        email: str = "anna.schmidt@example.org", *, roles: Iterable[UserAccountRoleName] = ()
    ) -> UserAccount:
        party = await make_person()
        view = await create_user_account(session, party_id=party.id, email=email, roles=roles, actor=Operator.CLI)
        view.account.password_hash = hash_secret(password)
        await session.flush()
        return view.account

    return _make_login_account


@pytest.fixture
def grants(session: AsyncSession) -> Callable[[], Awaitable[set[tuple[str, GrantMode]]]]:
    """Read every client scope grant as ``(scope_key, mode)`` straight from the database."""

    async def _grants() -> set[tuple[str, GrantMode]]:
        rows = await session.execute(select(ApplicationClientScopeGrant.scope_key, ApplicationClientScopeGrant.mode))
        return set(rows.tuples())

    return _grants


@pytest.fixture
def scope_grant_details(session: AsyncSession) -> Callable[[], Awaitable[Counter[str | None]]]:
    """Read the details of the grant and revocation audit entries.

    A ``Counter``: entries written in one transaction share their timestamp, so the log has no order to compare.
    """

    async def _scope_grant_details() -> Counter[str | None]:
        statement = select(AuthAuditLog.detail).where(
            AuthAuditLog.event_type.in_([AuditEventType.SCOPE_GRANT_ADDED, AuditEventType.SCOPE_GRANT_REMOVED])
        )
        return Counter((await session.execute(statement)).scalars())

    return _scope_grant_details


@pytest.fixture
def make_person(session: AsyncSession) -> Callable[..., Awaitable[Party]]:
    """Create a person party straight through the models; the CRM API is not what is under test."""

    async def _make_person(firstname: str = "Anna", lastname: str = "Schmidt") -> Party:
        party = Party(id=uuid4(), type=PartyType.PERSON, person=Person(firstname=firstname, lastname=lastname))
        session.add(party)
        await session.flush()
        return party

    return _make_person


@pytest.fixture
def make_company(session: AsyncSession) -> Callable[..., Awaitable[Party]]:
    async def _make_company(name: str = "Musterfirma GmbH") -> Party:
        party = Party(id=uuid4(), type=PartyType.COMPANY, company=Company(name=name))
        session.add(party)
        await session.flush()
        return party

    return _make_company


@pytest.fixture
async def application_client(session: AsyncSession) -> ApplicationClient:
    """The client the sessions of ``add_user_session`` belong to."""
    client = ApplicationClient(
        client_id=f"portal-{uuid4().hex[:8]}", name="Portal", status=ApplicationClientStatus.ACTIVE
    )
    session.add(client)
    await session.flush()
    return client


@pytest.fixture
def add_user_session(
    session: AsyncSession, application_client: ApplicationClient
) -> Callable[[UUID], Awaitable[UserSession]]:
    """Insert a live session for an account straight through the model, without a login.

    The hash is an arbitrary unique string, never anything that looks like a real refresh token.
    """

    async def _add_user_session(user_account_id: UUID) -> UserSession:
        user_session = UserSession(
            user_account_id=user_account_id,
            application_client_id=application_client.id,
            scope="account:self crm:read:own",
            refresh_token_hash=f"test-hash-{uuid4().hex}",
            expires_at=FAR_FUTURE,
        )
        session.add(user_session)
        await session.flush()
        return user_session

    return _add_user_session


@pytest.fixture
def audit_events(session: AsyncSession) -> Callable[[UUID], Awaitable[Counter[str]]]:
    """Count the audit entries of one user account by event type; each names the account as a `user`."""

    async def _audit_events(user_id: UUID) -> Counter[str]:
        statement = select(AuthAuditLog.principal_type, AuthAuditLog.event_type).where(
            AuthAuditLog.principal_id == str(user_id)
        )
        rows = (await session.execute(statement)).tuples().all()
        assert {principal_type for principal_type, _ in rows} <= {PrincipalType.USER}
        return Counter(event_type for _, event_type in rows)

    return _audit_events


@pytest.fixture
def audit_rows(session: AsyncSession) -> Callable[..., Awaitable[list[AuthAuditLog]]]:
    """Read the audit entries of one event type, optionally only those naming one principal type."""

    async def _audit_rows(
        event_type: AuditEventType, principal_type: PrincipalType | None = None
    ) -> list[AuthAuditLog]:
        statement = select(AuthAuditLog).where(AuthAuditLog.event_type == event_type)
        if principal_type is not None:
            statement = statement.where(AuthAuditLog.principal_type == principal_type)
        return list(await session.scalars(statement))

    return _audit_rows


@pytest.fixture
def live_tokens(session: AsyncSession) -> Callable[[UUID], Awaitable[list[UserActionToken]]]:
    """Read the unused, not invalidated action tokens of one account as the database holds them now."""

    async def _live_tokens(user_id: UUID) -> list[UserActionToken]:
        statement = select(UserActionToken).where(
            UserActionToken.user_account_id == user_id,
            UserActionToken.used_at.is_(None),
            UserActionToken.invalidated_at.is_(None),
        )
        return list(await session.scalars(statement.execution_options(populate_existing=True)))

    return _live_tokens


@pytest.fixture
def password() -> str:
    """A password the policy accepts."""
    return "correct horse battery staple"


@pytest.fixture
def command_session(session: AsyncSession, monkeypatch) -> None:
    """Run the operator commands on the test's ``session``, in a SAVEPOINT an error rolls back - as their own
    transaction."""

    @asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        async with session.begin_nested():
            yield session

    monkeypatch.setattr(bootstrap, "_session", _session)
