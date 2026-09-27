"""The token endpoint's grants: ``client_credentials`` for a client itself, ``password`` and ``refresh_token``
for a person on whose behalf a client acts (user-authentication spec, "Tokens").

Every grant authenticates the client first. The person grants return a ``TokenDenial`` instead of raising
(decision O): the failed-login counter, a revoked session and the audit entry of the denial must commit.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.config import AuthSettings
from app.core.auth.inputs import normalize_email
from app.core.auth.passwords import verify_dummy_password
from app.core.auth.principal import ApplicationPrincipal, PasswordLogin, PrincipalType, UserPrincipal
from app.core.auth.roles import Role, scopes_for
from app.core.auth.scopes import Scope, parse_scopes
from app.core.auth.secrets import digest, verify_and_update_async, verify_secret_async
from app.core.auth.tokens import CreatedAccessToken, create_access_token, create_application_access_token
from app.core.db.models import (
    ApplicationClient,
    ApplicationClientSecret,
    ApplicationClientStatus,
    GrantMode,
    UserAccount,
    UserAccountStatus,
    UserSession,
)

from .accounts import find_user_account_by_email, get_user_account, lock_user_account
from .audit import AuditEventType, write_auth_audit_log
from .clients import find_application_client
from .errors import InvalidClientCredentialsError, InvalidClientScopeError, UserAccountNotFoundError
from .results import IssuedUserToken, TokenDenial, UserTokenResult
from .roles import account_roles
from .scopes import granted_active_scope_keys, resolve_token_scopes
from .secrets import is_secret_usable, normalize_datetime
from .sessions import (
    RefreshTokenState,
    SessionRevokedReason,
    lock_session_by_refresh_token,
    open_session,
    refresh_token_state,
    revoke_session,
    rotate_session,
)

INVALID_CLIENT_CREDENTIALS = "Invalid client credentials"
MAX_CLIENT_CREDENTIAL_LENGTH = 255
"""The longest ``client_id`` or ``client_secret`` worth looking at. A secret is 51 characters; a longer value
is ``invalid_client`` at once - never hashed, never looked up, never written into the audit log."""
RACED_REFRESH_DETAIL = "refresh token reused within grace"
"""The fixed ``token.denied`` detail of a rotated-out refresh token presented within ``REFRESH_REUSE_GRACE``."""


@dataclass(frozen=True)
class _AuthenticatedClient:
    """A client that proved who it is, and the secret it proved it with."""

    client: ApplicationClient
    secret: ApplicationClientSecret

    def granted(self, mode: GrantMode) -> frozenset[str]:
        return granted_active_scope_keys(self.client.scope_grants, mode=mode)

    @property
    def actor(self) -> ApplicationPrincipal:
        """The client as the actor of a revocation its request causes."""
        return ApplicationPrincipal(
            principal_id=self.client.id, client_id=self.client.client_id, scopes=self.granted(GrantMode.APPLICATION)
        )


async def issue_client_token(
    session: AsyncSession,
    settings: AuthSettings,
    *,
    client_id: str,
    client_secret: str,
    requested_scopes: Iterable[str] | str | None = None,
    now: datetime | None = None,
) -> CreatedAccessToken:
    issued_at = normalize_datetime(now or datetime.now(UTC))
    authenticated = await _authenticate_client(session, client_id=client_id, client_secret=client_secret, now=issued_at)
    if authenticated is None:
        raise InvalidClientCredentialsError(INVALID_CLIENT_CREDENTIALS)

    client = authenticated.client
    try:
        token_scopes = resolve_token_scopes(
            requested=parse_scopes(requested_scopes), granted=authenticated.granted(GrantMode.APPLICATION)
        )
    except InvalidClientScopeError as exc:
        await _deny_client(session, client.id, str(exc))
        raise

    authenticated.secret.last_used_at = issued_at
    token = create_application_access_token(
        settings,
        principal_id=client.id,
        client_id=client.client_id,
        scopes=token_scopes,
        now=issued_at,
    )
    await write_auth_audit_log(
        session,
        principal_type=PrincipalType.APPLICATION,
        principal_id=client.id,
        event_type=AuditEventType.TOKEN_ISSUED,
        success=True,
        detail=f"Issued client credentials token for {client.client_id}.",
    )

    await session.flush()
    return token


async def issue_user_token(
    session: AsyncSession,
    settings: AuthSettings,
    *,
    client_id: str,
    client_secret: str,
    username: str,
    password: str,
    requested_scopes: Iterable[str] | str | None = None,
    now: datetime | None = None,
) -> UserTokenResult:
    """The ``password`` grant: log a person in through a client and open a session.

    Unknown address, disabled account, no password, a lock and a wrong password are one ``INVALID_GRANT``
    that takes as long as a wrong password. A wrong password counts towards the lock, which is persisted
    although the login is denied.
    """
    issued_at = normalize_datetime(now or datetime.now(UTC))
    authenticated = await _authenticate_login_client(
        session, client_id=client_id, client_secret=client_secret, now=issued_at
    )
    if isinstance(authenticated, TokenDenial):
        return authenticated

    # Read without a lock and verify without one: a lock held through the hash would make attempts on an
    # existing address queue behind each other - slower than on an unknown one (enumeration) and holding a
    # pooled connection each. Exactly one Argon2 verification runs on every path.
    found = await _find_login_account(session, username)
    if found is None:
        return await _refuse_login(session, None, "unknown account", password=password)
    if (refusal := _login_refusal(found, now=issued_at)) is not None:
        return await _refuse_login(session, found.id, refusal, password=password)
    if found.password_hash is None:
        return await _refuse_login(session, found.id, "no password set", password=password)

    checked_hash = found.password_hash
    verified, upgraded_hash = await verify_and_update_async(password, checked_hash)

    # Only now the row lock, and the outcome applied to the row as it is under it: concurrent wrong passwords
    # each re-read the counter the one before wrote, so all of them count. What changed since the unlocked
    # read wins - disabled, locked by another attempt, or a new password (a reset redeemed meanwhile): the
    # answer is the same ``invalid_grant`` and the verification, made against a stale state, counts for nothing.
    try:
        account = await lock_user_account(session, found.id)
    except UserAccountNotFoundError:
        return await _deny_user(session, None, "account deleted during login", TokenDenial.INVALID_GRANT)
    if _login_refusal(account, now=issued_at) is not None or account.password_hash != checked_hash:
        return await _deny_user(session, account.id, "account changed during login", TokenDenial.INVALID_GRANT)
    if not verified:
        _count_failed_login(account, settings, now=issued_at)
        return await _deny_user(session, account.id, "wrong password", TokenDenial.INVALID_GRANT)

    account.failed_login_count = 0
    account.locked_until = None
    if upgraded_hash is not None:
        account.password_hash = upgraded_hash

    roles = await account_roles(session, account)
    try:
        token_scopes = resolve_token_scopes(
            requested=parse_scopes(requested_scopes),
            granted=authenticated.granted(GrantMode.DELEGATED),
            ceilings=[scopes_for(roles)],
        )
    except InvalidClientScopeError as exc:
        return await _deny_user(session, account.id, str(exc), TokenDenial.INVALID_SCOPE)

    account.last_login_at = issued_at
    opened = await open_session(
        session,
        user_id=account.id,
        application_client_id=authenticated.client.id,
        scope=token_scopes,
        expires_at=issued_at + timedelta(days=settings.refresh_token_expire_days),
    )
    return await _issue(
        session,
        settings,
        authenticated,
        account=account,
        user_session=opened.user_session,
        refresh_token=opened.plaintext,
        roles=roles,
        scopes=token_scopes,
        now=issued_at,
        detail=f"Issued a password login token through client {authenticated.client.client_id}.",
    )


async def refresh_user_token(
    session: AsyncSession,
    settings: AuthSettings,
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    requested_scopes: Iterable[str] | str | None = None,
    now: datetime | None = None,
) -> UserTokenResult:
    """The ``refresh_token`` grant: rotate a session's refresh token and issue a fresh access token.

    The scopes are computed anew - roles re-derived, the session's ``scope`` an additional ceiling that is
    never rewritten. A rotated-out token is refused: within ``REFRESH_REUSE_GRACE`` as a race, later as a
    replay that ends the session. The login lock does not apply.
    """
    issued_at = normalize_datetime(now or datetime.now(UTC))
    authenticated = await _authenticate_login_client(
        session, client_id=client_id, client_secret=client_secret, now=issued_at
    )
    if isinstance(authenticated, TokenDenial):
        return authenticated

    token_digest = digest(refresh_token)
    user_session = await lock_session_by_refresh_token(
        session, token_digest, application_client_id=authenticated.client.id
    )
    if user_session is None:
        return await _deny_user(session, None, "unknown refresh token", TokenDenial.INVALID_GRANT)

    state = refresh_token_state(user_session, token_digest, now=issued_at)
    if state is RefreshTokenState.ENDED:
        return await _deny_user(
            session, user_session.user_account_id, "refresh token of an ended session", TokenDenial.INVALID_GRANT
        )

    # A disabled account ends the session whichever of its tokens arrives.
    account = await get_user_account(session, user_session.user_account_id)
    if account.status is UserAccountStatus.DISABLED:
        await revoke_session(
            session,
            user_session,
            reason=SessionRevokedReason.ACCOUNT_DISABLED,
            actor=authenticated.actor,
            now=issued_at,
        )
        return TokenDenial.INVALID_GRANT

    match state:
        case RefreshTokenState.RACED:
            return await _deny_user(session, account.id, RACED_REFRESH_DETAIL, TokenDenial.INVALID_GRANT)
        case RefreshTokenState.REUSED:
            await revoke_session(
                session,
                user_session,
                reason=SessionRevokedReason.REUSE_DETECTED,
                actor=authenticated.actor,
                now=issued_at,
            )
            return TokenDenial.INVALID_GRANT
        case RefreshTokenState.CURRENT:
            pass

    roles = await account_roles(session, account)
    try:
        token_scopes = resolve_token_scopes(
            requested=parse_scopes(requested_scopes),
            granted=authenticated.granted(GrantMode.DELEGATED),
            ceilings=[scopes_for(roles), parse_scopes(user_session.scope)],
        )
    except InvalidClientScopeError as exc:
        return await _deny_user(session, account.id, str(exc), TokenDenial.INVALID_SCOPE)

    return await _issue(
        session,
        settings,
        authenticated,
        account=account,
        user_session=user_session,
        refresh_token=rotate_session(user_session, now=issued_at),
        roles=roles,
        scopes=token_scopes,
        now=issued_at,
        detail=f"Refreshed session {user_session.id} through client {authenticated.client.client_id}.",
    )


async def _issue(
    session: AsyncSession,
    settings: AuthSettings,
    authenticated: _AuthenticatedClient,
    *,
    account: UserAccount,
    user_session: UserSession,
    refresh_token: str,
    roles: frozenset[Role],
    scopes: frozenset[str],
    now: datetime,
    detail: str,
) -> IssuedUserToken:
    """Mint the person's access token for ``user_session`` and record it."""
    authenticated.secret.last_used_at = now
    principal = UserPrincipal(
        principal_id=account.id,
        client_id=authenticated.client.client_id,
        scopes=scopes,
        party_id=account.party_id,
        roles=roles,
        login=PasswordLogin(session_id=user_session.id),
    )
    token = create_access_token(settings, principal, now=now)
    await write_auth_audit_log(
        session,
        principal_type=PrincipalType.USER,
        principal_id=account.id,
        event_type=AuditEventType.TOKEN_ISSUED,
        success=True,
        detail=detail,
    )
    return IssuedUserToken(
        token=token,
        refresh_token=refresh_token,
        refresh_expires_in=int((user_session.expires_at - now).total_seconds()),
    )


async def _authenticate_client(
    session: AsyncSession, *, client_id: str, client_secret: str, now: datetime
) -> _AuthenticatedClient | None:
    """Return the active client ``client_id`` if one of its usable secrets is ``client_secret``.

    ``None`` is ``invalid_client``, and its ``token.denied`` entry is written here. It names the client, or for
    an unknown one the submitted ``client_id`` - unless that is longer than ``MAX_CLIENT_CREDENTIAL_LENGTH``.
    """
    if len(client_id) > MAX_CLIENT_CREDENTIAL_LENGTH or len(client_secret) > MAX_CLIENT_CREDENTIAL_LENGTH:
        await _deny_client(session, None, INVALID_CLIENT_CREDENTIALS)
        return None

    client = await find_application_client(session, client_id)
    if client is not None and client.status == ApplicationClientStatus.ACTIVE:
        for secret in client.secrets:
            if is_secret_usable(secret, now=now) and await verify_secret_async(client_secret, secret.secret_hash):
                return _AuthenticatedClient(client=client, secret=secret)

    await _deny_client(session, client.id if client is not None else client_id, INVALID_CLIENT_CREDENTIALS)
    return None


async def _authenticate_login_client(
    session: AsyncSession, *, client_id: str, client_secret: str, now: datetime
) -> _AuthenticatedClient | TokenDenial:
    """Steps 1 and 2 of both person grants: the client proves who it is and holds ``auth:users:login`` in
    ``application`` mode."""
    authenticated = await _authenticate_client(session, client_id=client_id, client_secret=client_secret, now=now)
    if authenticated is None:
        return TokenDenial.INVALID_CLIENT
    if Scope.AUTH_USERS_LOGIN not in authenticated.granted(GrantMode.APPLICATION):
        await _deny_client(session, authenticated.client.id, "Client may not log people in")
        return TokenDenial.UNAUTHORIZED_CLIENT

    return authenticated


async def _find_login_account(session: AsyncSession, username: str) -> UserAccount | None:
    """The account ``username`` logs in to, unlocked; a username that is no address matches none."""
    try:
        email = normalize_email(username)
    except ValueError:
        return None

    return await find_user_account_by_email(session, email)


def _login_refusal(account: UserAccount, *, now: datetime) -> str | None:
    """Why ``account`` may not log in whatever the password, or ``None``: disabled or locked.

    A locked account's counter does not move - the password is not even checked.
    """
    if account.status is UserAccountStatus.DISABLED:
        return "account disabled"
    if account.locked_until is not None and account.locked_until > now:
        return "account locked"

    return None


async def _refuse_login(session: AsyncSession, user_id: uuid.UUID | None, detail: str, *, password: str) -> TokenDenial:
    """Refuse a login before its password is checked - as slowly as a wrong password (no enumeration)."""
    await verify_dummy_password(password)
    return await _deny_user(session, user_id, detail, TokenDenial.INVALID_GRANT)


def _count_failed_login(account: UserAccount, settings: AuthSettings, *, now: datetime) -> None:
    """Count a wrong password; from the threshold on, lock for 1 minute, doubling per failure up to the maximum."""
    account.failed_login_count += 1
    beyond = account.failed_login_count - settings.login_lockout_threshold
    if beyond >= 0:
        account.locked_until = now + timedelta(minutes=min(settings.login_lockout_max_minutes, 2**beyond))


async def _deny_client(session: AsyncSession, principal_id: uuid.UUID | str | None, detail: str) -> None:
    """Record a request the client itself was refused for (steps 1 and 2 of every grant)."""
    await write_auth_audit_log(
        session,
        principal_type=PrincipalType.APPLICATION,
        principal_id=principal_id,
        event_type=AuditEventType.TOKEN_DENIED,
        success=False,
        detail=detail,
    )


async def _deny_user(session: AsyncSession, user_id: uuid.UUID | None, detail: str, denial: TokenDenial) -> TokenDenial:
    """Record a refused person grant and return ``denial``.

    The entry names the account, or ``None`` when none matched - never the submitted username.
    """
    await write_auth_audit_log(
        session,
        principal_type=PrincipalType.USER,
        principal_id=user_id,
        event_type=AuditEventType.TOKEN_DENIED,
        success=False,
        detail=detail,
    )
    return denial
