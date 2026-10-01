"""The OAuth2 token endpoint: form-encoded, OAuth2 error codes (RFC 6749, section 5.2).

Four grants, each behind its own dependency seam: ``client_credentials`` for a client itself, ``password``
and ``refresh_token`` for a person the client logs in, and the Discord-user exchange - an extension grant
(RFC 6749, section 4.5) - for a person the client vouches for. The form is parsed into one of four grant types
at the boundary; ``create_token`` dispatches on it and is the one place a denial becomes an error response.
"""

import base64
import binascii
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from fastapi.security.utils import get_authorization_scheme_param
from pydantic import TypeAdapter, ValidationError

from app.api.v1.common import ApiError, DBSession, error_responses
from app.core.auth import CreatedAccessToken
from app.core.auth.dependencies import AuthConfig
from app.core.auth.inputs import MAX_EMAIL_LENGTH, DiscordUserId
from app.core.auth.passwords import MAX_PASSWORD_LENGTH
from app.core.logging import bind_request_log_context
from app.services.auth import (
    ExchangeResult,
    InvalidClientCredentialsError,
    InvalidClientScopeError,
    TokenDenial,
    UserTokenResult,
    exchange_discord_user,
    issue_client_token,
    issue_user_token,
    refresh_user_token,
)
from app.services.auth.tokens import MAX_CLIENT_CREDENTIAL_LENGTH

from .errors import (
    INVALID_CLIENT,
    INVALID_GRANT,
    INVALID_REQUEST,
    INVALID_SCOPE,
    UNAUTHORIZED_CLIENT,
    UNSUPPORTED_GRANT_TYPE,
)
from .schemas import AccessTokenResponse

router = APIRouter()

IssueClientToken = Callable[..., Awaitable[CreatedAccessToken]]
UserGrant = Callable[..., Awaitable[UserTokenResult]]
"""The service behind a person's login grant: ``issue_user_token`` or ``refresh_user_token``."""
ExchangeDiscordUser = Callable[..., Awaitable[ExchangeResult]]
"""The service behind the Discord-user grant: ``exchange_discord_user``."""


class GrantType(StrEnum):
    """The ``grant_type`` values the endpoint supports."""

    CLIENT_CREDENTIALS = "client_credentials"
    PASSWORD = "password"
    REFRESH_TOKEN = "refresh_token"
    DISCORD_USER = "urn:skillforge:params:oauth:grant-type:discord-user"


@dataclass(frozen=True)
class ClientCredentialsGrant:
    """A client asks for a token of its own; it needs no parameter beyond its credentials."""


@dataclass(frozen=True)
class PasswordGrant:
    """A client logs a person in with their e-mail address and password."""

    username: str
    password: str


@dataclass(frozen=True)
class RefreshTokenGrant:
    """A client renews a person's session with its refresh token."""

    refresh_token: str


@dataclass(frozen=True)
class DiscordUserGrant:
    """A client vouches for the person a Discord user is linked to and asks for that person's token."""

    discord_user_id: int


type Grant = ClientCredentialsGrant | PasswordGrant | RefreshTokenGrant | DiscordUserGrant


@dataclass(frozen=True)
class TokenForm:
    """The token request, parsed: who the client is, the scope it asks for, and the grant with its parameters."""

    client_id: str
    client_secret: str
    scope: str | None
    grant: Grant


_DENIALS: dict[TokenDenial, tuple[ApiError, str]] = {
    TokenDenial.INVALID_CLIENT: (INVALID_CLIENT, "invalid_client_credentials"),
    TokenDenial.UNAUTHORIZED_CLIENT: (UNAUTHORIZED_CLIENT, "unauthorized_client"),
    TokenDenial.INVALID_GRANT: (INVALID_GRANT, "invalid_grant"),
    TokenDenial.INVALID_SCOPE: (INVALID_SCOPE, "invalid_requested_scope"),
}
"""The answer to each denial, whichever grant it ended, and the ``auth_reason`` the request log gives."""

_DISCORD_USER_ID: TypeAdapter[int] = TypeAdapter(DiscordUserId)
"""Parses the ``discord_user_id`` form field: a decimal string within the ``BIGINT`` range, nothing else."""


def get_issue_client_token() -> IssueClientToken:
    return issue_client_token


def get_issue_user_token() -> UserGrant:
    return issue_user_token


def get_refresh_user_token() -> UserGrant:
    return refresh_user_token


def get_exchange_discord_user() -> ExchangeDiscordUser:
    return exchange_discord_user


async def get_token_form(
    request: Request,
    grant_type: Annotated[
        str,
        Form(
            description="`client_credentials`, `password` (a person's login), `refresh_token` or "
            "`urn:skillforge:params:oauth:grant-type:discord-user` (a person's token for a linked Discord user)."
        ),
    ],
    client_id: Annotated[
        str | None, Form(description="The client's ID, unless it authenticates with HTTP Basic, which wins.")
    ] = None,
    client_secret: Annotated[
        str | None, Form(description="The client's secret, unless it authenticates with HTTP Basic.")
    ] = None,
    scope: Annotated[
        str | None,
        Form(description="Space-separated scopes to narrow the token to; without it the token gets all it may hold."),
    ] = None,
    username: Annotated[
        str | None,
        Form(
            description="`password` grant only, required there: the person's login e-mail address, at most 254 "
            "characters."
        ),
    ] = None,
    password: Annotated[
        str | None,
        Form(description="`password` grant only, required there: the person's password, at most 128 characters."),
    ] = None,
    refresh_token: Annotated[
        str | None, Form(description="`refresh_token` grant only, required there: the session's current refresh token.")
    ] = None,
    discord_user_id: Annotated[
        str | None,
        Form(
            description="`urn:skillforge:params:oauth:grant-type:discord-user` grant only, required there: the "
            "Discord user who sent the command, as a decimal string."
        ),
    ] = None,
) -> TokenForm:
    try:
        parsed_grant_type = GrantType(grant_type)
    except ValueError:
        bind_request_log_context(request, auth_reason="unsupported_grant_type")
        raise UNSUPPORTED_GRANT_TYPE.exception() from None

    resolved_client_id, resolved_client_secret = _get_basic_credentials(request) or (client_id, client_secret)
    if not resolved_client_id or not resolved_client_secret:
        bind_request_log_context(request, auth_reason="missing_client_credentials")
        raise INVALID_REQUEST.exception()

    grant = _parse_grant(
        parsed_grant_type,
        username=username,
        password=password,
        refresh_token=refresh_token,
        discord_user_id=discord_user_id,
    )
    if grant is None:
        bind_request_log_context(
            request, auth_reason="invalid_grant_parameters", **_client_log_context(resolved_client_id)
        )
        raise INVALID_REQUEST.exception()

    return TokenForm(client_id=resolved_client_id, client_secret=resolved_client_secret, scope=scope, grant=grant)


def _parse_grant(
    grant_type: GrantType,
    *,
    username: str | None,
    password: str | None,
    refresh_token: str | None,
    discord_user_id: str | None,
) -> Grant | None:
    """The grant with the parameters it needs, or ``None`` when one of them is missing, over-long or malformed.

    No login address is longer than ``MAX_EMAIL_LENGTH`` and no stored password longer than
    ``MAX_PASSWORD_LENGTH``: a longer value is refused before any look-up or hash, alike for every account.
    A Discord user ID is a decimal string of at most ``2^63 - 1`` - anything else is refused before any look-up.
    """
    match grant_type:
        case GrantType.CLIENT_CREDENTIALS:
            return ClientCredentialsGrant()
        case GrantType.PASSWORD if (
            username and password and len(username) <= MAX_EMAIL_LENGTH and len(password) <= MAX_PASSWORD_LENGTH
        ):
            return PasswordGrant(username=username, password=password)
        case GrantType.REFRESH_TOKEN if refresh_token:
            return RefreshTokenGrant(refresh_token=refresh_token)
        case GrantType.DISCORD_USER:
            user_id = _parse_discord_user_id(discord_user_id)
            return None if user_id is None else DiscordUserGrant(discord_user_id=user_id)
        case _:
            return None


def _parse_discord_user_id(value: str | None) -> int | None:
    """``value`` as a Discord user ID, or ``None`` when it is missing or no decimal string of ``0 .. 2^63 - 1``."""
    if value is None:
        return None
    try:
        return _DISCORD_USER_ID.validate_python(value)
    except ValidationError:
        return None


def _get_basic_credentials(request: Request) -> tuple[str, str] | None:
    authorization = request.headers.get("Authorization")
    scheme, encoded = get_authorization_scheme_param(authorization)
    if scheme.lower() != "basic" or not encoded:
        return None

    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
    except binascii.Error, UnicodeDecodeError:
        return None

    if ":" not in decoded:
        return None

    username, password = decoded.split(":", 1)
    return username, password


@router.post(
    "/token",
    response_model=AccessTokenResponse,
    response_model_exclude_none=True,
    responses=error_responses(
        UNSUPPORTED_GRANT_TYPE, INVALID_SCOPE, INVALID_GRANT, UNAUTHORIZED_CLIENT, INVALID_CLIENT, INVALID_REQUEST
    ),
)
async def create_token(
    request: Request,
    session: DBSession,
    settings: AuthConfig,
    issue_client: Annotated[IssueClientToken, Depends(get_issue_client_token)],
    issue_user: Annotated[UserGrant, Depends(get_issue_user_token)],
    refresh_user: Annotated[UserGrant, Depends(get_refresh_user_token)],
    exchange: Annotated[ExchangeDiscordUser, Depends(get_exchange_discord_user)],
    form: Annotated[TokenForm, Depends(get_token_form)],
) -> AccessTokenResponse | JSONResponse:
    """Issue an access token.

    The client authenticates for every grant, with HTTP Basic or `client_id` / `client_secret` in the form.
    `client_credentials` issues a token of the client's own. `password` logs a person in and opens a session;
    `refresh_token` renews it and replaces its refresh token. Both need `auth:users:login` granted to the client
    in `application` mode. Every failure of the person's credentials or refresh token is the same
    `invalid_grant`.

    `urn:skillforge:params:oauth:grant-type:discord-user` exchanges the Discord user in `discord_user_id` - the one
    who sent a command, never one named in it - for a token of the person party it is actively linked to. It needs
    `auth:users:exchange` granted to the client in `application` mode. The token carries `amr: ["discord"]` and at
    most `crm:read`, `crm:read:own` and `crm:write`, within the client's `delegated` grants and the person's roles.
    No refresh token and no session: exchange again when it expires. No active link, no account and a disabled
    account are the same `invalid_grant`. Swagger UI's Authorize dialog cannot use this grant; try it with curl.
    """
    # Every denial is *returned*, not raised: the services have written its audit entry - and for a person
    # the failed-login counter or a revoked session - and raising would roll the session back.
    match form.grant:
        case ClientCredentialsGrant():
            try:
                token = await issue_client(
                    session,
                    settings,
                    client_id=form.client_id,
                    client_secret=form.client_secret,
                    requested_scopes=form.scope,
                )
            except InvalidClientCredentialsError:
                return _deny(request, TokenDenial.INVALID_CLIENT, form.client_id)
            except InvalidClientScopeError:
                return _deny(request, TokenDenial.INVALID_SCOPE, form.client_id)
            return AccessTokenResponse.from_created_token(token)
        case DiscordUserGrant(discord_user_id=discord_user_id):
            exchanged = await exchange(
                session,
                settings,
                client_id=form.client_id,
                client_secret=form.client_secret,
                discord_user_id=discord_user_id,
                requested_scopes=form.scope,
            )
            if isinstance(exchanged, TokenDenial):
                return _deny(request, exchanged, form.client_id)
            return AccessTokenResponse.from_created_token(exchanged)
        case PasswordGrant(username=username, password=password):
            result = await issue_user(
                session,
                settings,
                client_id=form.client_id,
                client_secret=form.client_secret,
                username=username,
                password=password,
                requested_scopes=form.scope,
            )
        case RefreshTokenGrant(refresh_token=refresh_token):
            result = await refresh_user(
                session,
                settings,
                client_id=form.client_id,
                client_secret=form.client_secret,
                refresh_token=refresh_token,
                requested_scopes=form.scope,
            )

    if isinstance(result, TokenDenial):
        return _deny(request, result, form.client_id)
    return AccessTokenResponse.from_issued_user_token(result)


def _deny(request: Request, denial: TokenDenial, client_id: str) -> JSONResponse:
    """Answer ``denial``, naming the reason in the request log - never the username or a secret."""
    error, auth_reason = _DENIALS[denial]
    bind_request_log_context(request, auth_reason=auth_reason, **_client_log_context(client_id))
    return error.response()


def _client_log_context(client_id: str) -> dict[str, str]:
    """The client as the request log names it: a value longer than any client's ID is left out, never bound."""
    return {"client_id": client_id} if len(client_id) <= MAX_CLIENT_CREDENTIAL_LENGTH else {}
