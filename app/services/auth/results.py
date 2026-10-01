from dataclasses import dataclass
from enum import StrEnum

from app.core.auth.roles import Role
from app.core.auth.tokens import CreatedAccessToken
from app.core.db.models import ApplicationClient, ApplicationClientSecret, UserAccount, UserActionToken, UserSession


@dataclass(frozen=True)
class CreatedClientSecret:
    plaintext: str
    secret: ApplicationClientSecret


@dataclass(frozen=True)
class BootstrappedApplicationClient:
    client: ApplicationClient
    created_client: bool
    created_secret: CreatedClientSecret | None
    granted_scopes: frozenset[str]


@dataclass(frozen=True)
class IssuedActionToken:
    """A one-time token as it leaves the service: the only place its plaintext exists.

    ``token`` holds only the digest, so the plaintext travels next to it - straight into the response
    or the terminal, never into a log or an audit entry.
    """

    plaintext: str
    token: UserActionToken


@dataclass(frozen=True)
class UserAccountWithRoles:
    """An account together with every role it holds: the stored ones plus the ones the CRM derives."""

    account: UserAccount
    roles: frozenset[Role]


@dataclass(frozen=True)
class BootstrappedAdminAccount:
    """What ``bootstrap_admin_account`` did: the account, whether it created it, and the token it issued."""

    account: UserAccount
    created_account: bool
    issued: IssuedActionToken


@dataclass(frozen=True)
class OpenedSession:
    """A new login session as it leaves the service: the only place its first refresh token's plaintext exists.

    ``user_session`` holds only the digest; the plaintext travels next to it, straight into the response.
    """

    plaintext: str
    user_session: UserSession


@dataclass(frozen=True)
class IssuedUserToken:
    """What a person's login or refresh hands out: the access token plus the session's new refresh token.

    ``refresh_token`` is the plaintext - it exists only here and in the response; the session stores its
    digest. ``refresh_expires_in`` counts the seconds until the session's absolute ``expires_at``.
    """

    token: CreatedAccessToken
    refresh_token: str
    refresh_expires_in: int


class TokenDenial(StrEnum):
    """Why a grant was refused: the OAuth2 error code (RFC 6749, section 5.2) of the answer.

    Returned, never raised (user-authentication spec, decision O): a denial may have written state - an
    audit entry, the failed-login counter, a revoked session - that raising would roll back.
    """

    INVALID_CLIENT = "invalid_client"
    UNAUTHORIZED_CLIENT = "unauthorized_client"
    INVALID_GRANT = "invalid_grant"
    INVALID_SCOPE = "invalid_scope"


type UserTokenResult = IssuedUserToken | TokenDenial
"""The outcome of ``issue_user_token`` and ``refresh_user_token``."""

type ExchangeResult = CreatedAccessToken | TokenDenial
"""The outcome of ``exchange_discord_user``: a person's access token - no refresh token, no session - or the denial."""
