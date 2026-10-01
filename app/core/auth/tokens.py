"""Access tokens: a ``Principal``, signed.

``create_access_token`` writes a principal into the claims of a JWT and ``validate_access_token``
reads it back. The claims are declared once, as one pydantic model per principal type that
``principal_type`` tells apart - issuing and validating use the same declaration, so they cannot
drift apart.
"""

import uuid
from abc import abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Self

import jwt
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    PlainSerializer,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .config import AuthSettings
from .principal import (
    ApplicationPrincipal,
    AuthMethod,
    DiscordLogin,
    Login,
    PasswordLogin,
    Principal,
    PrincipalType,
    UserPrincipal,
)
from .roles import Role
from .scopes import CLIENT_ONLY_SCOPES, VOUCHED_SCOPES, canonical, format_scopes, parse_scopes

TOKEN_TYPE_BEARER = "bearer"

# Checked by PyJWT itself; the claims that say who the token speaks for are the models' business.
_REGISTERED_CLAIMS = ["iss", "aud", "sub", "iat", "exp", "jti"]


class TokenValidationError(ValueError):
    """Raised when an access token cannot be trusted or converted into a principal."""


@dataclass(frozen=True)
class CreatedAccessToken:
    access_token: str
    token_type: str
    expires_at: datetime
    expires_in: int
    scope: str


def _parse_scope_claim(value: object) -> frozenset[str]:
    """The scope claim is an OAuth2 scope string - nothing else, not even a list of scopes - and is
    read in its canonical form."""
    if not isinstance(value, str):
        raise ValueError("scope must be a space-separated string")

    return canonical(parse_scopes(value))


def _not_empty(scopes: frozenset[str]) -> frozenset[str]:
    if not scopes:
        raise ValueError("scopes must not be empty")

    return scopes


_ScopeClaim = Annotated[
    frozenset[str],
    BeforeValidator(_parse_scope_claim),
    AfterValidator(_not_empty),
    PlainSerializer(format_scopes, return_type=str),
]
"""The ``scope`` claim: a space-separated string on the wire, a non-empty canonical set in Python."""

_RolesClaim = Annotated[frozenset[Role], PlainSerializer(sorted, return_type=list[Role])]
"""The ``roles`` claim: a sorted list on the wire; an unknown role makes the token invalid."""

_AuthMethodsClaim = Annotated[
    frozenset[AuthMethod],
    Field(min_length=1),
    PlainSerializer(sorted, return_type=list[AuthMethod]),
]
"""The ``amr`` claim (RFC 8176): a sorted, non-empty list on the wire; an unknown method makes the token invalid."""


class _Claims(BaseModel):
    """What every access token says about the principal it speaks for.

    The registered claims (``iss``, ``aud``, ``iat``, ``exp``, ``jti``) are PyJWT's to check and are
    ignored here.
    """

    sub: str
    principal_id: uuid.UUID
    azp: str = Field(min_length=1)
    scope: _ScopeClaim

    @abstractmethod
    def to_principal(self) -> Principal: ...

    @model_validator(mode="after")
    def _subject_names_the_principal(self) -> Self:
        # Compared with the canonical spelling: SkillForge writes ``sub`` from the principal, so a
        # token whose ``sub`` spells it any other way was not written by SkillForge.
        if self.sub != self.to_principal().subject:
            raise ValueError("sub does not name the principal of the token")

        return self


class _ApplicationClaims(_Claims):
    principal_type: Literal[PrincipalType.APPLICATION]

    def to_principal(self) -> ApplicationPrincipal:
        return ApplicationPrincipal(principal_id=self.principal_id, client_id=self.azp, scopes=self.scope)


class _UserClaims(_Claims):
    principal_type: Literal[PrincipalType.USER]
    party_id: uuid.UUID
    # Only a password login opens a session: a token obtained without one leaves ``sid`` out.
    sid: uuid.UUID | None = None
    roles: _RolesClaim
    amr: _AuthMethodsClaim

    @field_validator("sid", mode="before")
    @classmethod
    def _sid_is_left_out_never_null(cls, sid: object) -> object:
        # SkillForge leaves out the ``sid`` of a token without a session; a ``null`` was not written here.
        if sid is None:
            raise ValueError("sid is left out, never null")

        return sid

    @field_validator("scope")
    @classmethod
    def _carries_no_client_only_scope(cls, scope: frozenset[str]) -> frozenset[str]:
        # Client-only scopes are what a client may do for itself; a person's token never carries one.
        if scope & CLIENT_ONLY_SCOPES:
            raise ValueError("a person's token carries no client-only scope")

        return scope

    @model_validator(mode="after")
    def _carries_only_vouched_scopes_without_a_password(self) -> Self:
        # ``login`` refuses an ``amr`` and ``sid`` that name no way to log in. A token obtained without a password
        # carries nothing that needs one (bot-decoupling spec, decision R).
        if isinstance(self.login, DiscordLogin) and not self.scope <= VOUCHED_SCOPES:
            raise ValueError("a token obtained without a password carries vouched scopes only")

        return self

    @property
    def login(self) -> Login:
        """``amr`` and ``sid`` as one ``Login``: ``[pwd]`` with a session, ``[discord]`` without one - nothing else."""
        match sorted(self.amr), self.sid:
            case [AuthMethod.PASSWORD], uuid.UUID() as session_id:
                return PasswordLogin(session_id=session_id)
            case [AuthMethod.DISCORD], None:
                return DiscordLogin()
            case _:
                raise ValueError("amr and sid name no way to log in")

    def to_principal(self) -> UserPrincipal:
        return UserPrincipal(
            principal_id=self.principal_id,
            client_id=self.azp,
            scopes=self.scope,
            party_id=self.party_id,
            roles=self.roles,
            login=self.login,
        )


_ACCESS_CLAIMS: TypeAdapter[_ApplicationClaims | _UserClaims] = TypeAdapter(
    Annotated[_ApplicationClaims | _UserClaims, Field(discriminator="principal_type")]
)


def create_access_token(
    settings: AuthSettings,
    principal: Principal,
    *,
    now: datetime | None = None,
) -> CreatedAccessToken:
    """Issue an access token that speaks for ``principal``.

    The scope claim is canonical: a token never carries both a scope and its ``:own`` variant
    (ADR 0008). An empty scope is refused - every token grants something - and so is a client-only
    scope for a person, and any scope outside ``VOUCHED_SCOPES`` for a person without a password login.
    """
    issued_at = _normalize_datetime(now or datetime.now(UTC))
    expires_at = issued_at + timedelta(minutes=settings.access_token_expire_minutes)
    claims = _claims_of(principal)

    access_token = jwt.encode(
        {
            "iss": settings.issuer,
            "aud": settings.audience,
            **claims,
            "iat": issued_at,
            "exp": expires_at,
            "jti": str(uuid.uuid4()),
        },
        settings.secret_key.get_secret_value(),
        algorithm=settings.algorithm,
    )

    return CreatedAccessToken(
        access_token=access_token,
        token_type=TOKEN_TYPE_BEARER,
        expires_at=expires_at,
        expires_in=int((expires_at - issued_at).total_seconds()),
        scope=claims["scope"],
    )


def create_application_access_token(
    settings: AuthSettings,
    *,
    principal_id: uuid.UUID,
    client_id: str,
    scopes: Iterable[str] | str,
    now: datetime | None = None,
) -> CreatedAccessToken:
    """Shorthand for ``create_access_token`` with an ``ApplicationPrincipal``, for the callers that
    mint an application token straight from its parts."""
    principal = ApplicationPrincipal(principal_id=principal_id, client_id=client_id, scopes=parse_scopes(scopes))
    return create_access_token(settings, principal, now=now)


def validate_access_token(token: str, settings: AuthSettings) -> Principal:
    """Return the principal ``token`` speaks for; any token SkillForge did not issue as it stands is a
    ``TokenValidationError``."""
    try:
        claims = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[settings.algorithm],
            issuer=settings.issuer,
            audience=settings.audience,
            options={"require": _REGISTERED_CLAIMS},
        )
        principal_claims = _ACCESS_CLAIMS.validate_python(claims)
    except (jwt.PyJWTError, ValidationError) as exc:
        raise TokenValidationError("Invalid access token") from exc

    return principal_claims.to_principal()


def _claims_of(principal: Principal) -> dict[str, Any]:
    """The claims that say who ``principal`` is, validated and in their wire form."""
    claims: dict[str, object] = {
        "sub": principal.subject,
        "principal_type": principal.principal_type,
        "principal_id": principal.principal_id,
        "azp": principal.client_id,
        "scope": format_scopes(principal.scopes),
    }
    if isinstance(principal, UserPrincipal):
        claims |= {"party_id": principal.party_id, "roles": principal.roles, **_login_claims(principal.login)}

    # ``exclude_none``: a token without a session leaves ``sid`` out instead of writing ``null``.
    return _ACCESS_CLAIMS.dump_python(_ACCESS_CLAIMS.validate_python(claims), mode="json", exclude_none=True)


def _login_claims(login: Login) -> dict[str, object]:
    """``login`` as the ``amr`` and ``sid`` claims: only a password login names its session."""
    match login:
        case PasswordLogin(session_id=session_id):
            return {"amr": [AuthMethod.PASSWORD], "sid": session_id}
        case DiscordLogin():
            return {"amr": [AuthMethod.DISCORD]}


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)
