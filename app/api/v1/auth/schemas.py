from datetime import datetime
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.experimental.missing_sentinel import MISSING

from app.api.v1.common import ApiModel
from app.core.auth.inputs import DiscordUserId, LoginEmail
from app.core.auth.principal import Principal, UserPrincipal
from app.core.auth.roles import Role
from app.core.auth.tokens import CreatedAccessToken
from app.core.db.models import (
    ApplicationClient,
    ApplicationClientStatus,
    DiscordAccount,
    GrantMode,
    UserAccountRoleName,
    UserAccountStatus,
)
from app.services.auth.results import CreatedClientSecret, IssuedActionToken, IssuedUserToken, UserAccountWithRoles


class AccessTokenResponse(ApiModel):
    """A successful token response (RFC 6749, section 5.1). A person's grants add the session's refresh token."""

    access_token: str
    """The access token: a signed JWT to send as `Authorization: Bearer <token>`."""
    token_type: str
    """Always `bearer`."""
    expires_in: int
    """Seconds until the access token expires."""
    scope: str
    """Scopes the token grants, space-separated and canonical: an unqualified scope implies its `:own` form."""
    refresh_token: str | None = None
    """Only for the `password` and `refresh_token` grants: the session's new refresh token. It replaces the one
    presented, which stops working; SkillForge stores only its hash, so this is the one time it can be read."""
    refresh_expires_in: int | None = None
    """Only with `refresh_token`: seconds until the session ends for good - it expires absolutely, refreshing
    does not extend it."""

    @classmethod
    def from_created_token(cls, token: CreatedAccessToken) -> AccessTokenResponse:
        return cls(
            access_token=token.access_token,
            token_type=token.token_type,
            expires_in=token.expires_in,
            scope=token.scope,
        )

    @classmethod
    def from_issued_user_token(cls, issued: IssuedUserToken) -> AccessTokenResponse:
        token = issued.token
        return cls(
            access_token=token.access_token,
            token_type=token.token_type,
            expires_in=token.expires_in,
            scope=token.scope,
            refresh_token=issued.refresh_token,
            refresh_expires_in=issued.refresh_expires_in,
        )


class MeResponse(ApiModel):
    """What the calling token says about its bearer."""

    principal_type: str
    """Kind of principal the token was issued to: `application` for an application client, `user` for a person."""
    client_id: str | None
    """Client ID of the application client, or for a person the client that logged them in."""
    scopes: list[str]
    """Scopes the token grants, sorted and canonical: an unqualified scope implies its `:own` form."""
    user_id: UUID | None
    """User account of the person the token speaks for; `null` for an application principal."""
    party_id: UUID | None
    """CRM party of that person; `null` for an application principal."""
    roles: list[Role]
    """Roles the person holds, sorted - informational, which views to offer; empty for an application principal."""

    @classmethod
    def from_principal(cls, principal: Principal) -> MeResponse:
        person = principal if isinstance(principal, UserPrincipal) else None
        return cls(
            principal_type=principal.principal_type,
            client_id=principal.client_id,
            scopes=sorted(principal.scopes),
            user_id=person.principal_id if person else None,
            party_id=person.party_id if person else None,
            roles=sorted(person.roles) if person else [],
        )


class ApplicationClientCreateRequest(BaseModel):
    client_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None


class ApplicationClientUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    status: ApplicationClientStatus | None = None


class ApplicationClientSecretCreateRequest(BaseModel):
    label: str | None = None
    expires_at: datetime | None = None


class ApplicationClientScopeGrantRequest(ApiModel):
    """Scopes to grant an application client in one mode (ADR 0008)."""

    scopes: list[str] = Field(min_length=1)
    """Scopes to grant, e.g. `crm:read`. A scope the client already holds in `mode` stays as it is; if one scope is
    refused, none is granted."""
    mode: GrantMode
    """`application`: what the client may do for itself (`client_credentials`). `delegated`: the most it may do for a
    person it acts for; a client-only scope such as `auth:users:login` is refused in this mode."""


class ApplicationClientSecretResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    label: str | None
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApplicationClientResponse(ApiModel):
    """An application client with its scope grants and secrets."""

    id: UUID
    """ID of the client; its application tokens carry it as `principal_id`."""
    client_id: str
    """Identifier the client authenticates with at `POST /auth/token`."""
    name: str
    """Display name of the client."""
    description: str | None
    """Free-text description; `null` when there is none."""
    status: ApplicationClientStatus
    """An `active` client obtains tokens, a `disabled` one does not."""
    application_scopes: list[str]
    """Scopes granted in `application` mode - what the client may do for itself - sorted."""
    delegated_scopes: list[str]
    """Scopes granted in `delegated` mode - the most the client may do for a person - sorted."""
    secrets: list[ApplicationClientSecretResponse]
    """Secrets of the client, oldest first, without their values."""
    created_at: datetime
    """When the client was created."""
    updated_at: datetime
    """When the client was last changed."""

    @classmethod
    def from_model(cls, client: ApplicationClient) -> ApplicationClientResponse:
        return cls(
            id=client.id,
            client_id=client.client_id,
            name=client.name,
            description=client.description,
            status=client.status,
            application_scopes=_granted_scopes(client, GrantMode.APPLICATION),
            delegated_scopes=_granted_scopes(client, GrantMode.DELEGATED),
            secrets=[
                ApplicationClientSecretResponse.model_validate(secret)
                for secret in sorted(client.secrets, key=lambda item: item.created_at)
            ],
            created_at=client.created_at,
            updated_at=client.updated_at,
        )


def _granted_scopes(client: ApplicationClient, mode: GrantMode) -> list[str]:
    return sorted(grant.scope_key for grant in client.scope_grants if grant.mode == mode)


class CreatedClientSecretResponse(BaseModel):
    client_secret: str
    secret: ApplicationClientSecretResponse

    @classmethod
    def from_created_secret(cls, created_secret: CreatedClientSecret) -> CreatedClientSecretResponse:
        return cls(
            client_secret=created_secret.plaintext,
            secret=ApplicationClientSecretResponse.model_validate(created_secret.secret),
        )


class UserAccountCreateRequest(ApiModel):
    """Body of `POST /users`: the person party an account is created for."""

    party_id: UUID = Field(examples=["7d9f4f3e-1c2b-4a5d-9e8f-0a1b2c3d4e5f"])
    """ID of the person party the account belongs to. A company cannot hold one, a party at most one."""
    email: LoginEmail | None = Field(None, examples=["anna.schmidt@example.org"])
    """Login e-mail address, needed for the password login. Stored lowercased, unique across all accounts. Omit
    it or send `null` for an account without one."""
    roles: list[UserAccountRoleName] = Field(default_factory=list, examples=[["admin"]])
    """Stored roles to give the account. The other roles follow from the CRM and cannot be set."""


class UserAccountUpdateRequest(ApiModel):
    """Body of `PATCH /users/{user_id}`: only the fields that are sent change."""

    email: LoginEmail | None | MISSING = MISSING
    """New login e-mail address, or `null` to remove it - refused while the account has a password. A change
    invalidates the account's unused invitation and reset tokens."""
    status: UserAccountStatus | MISSING = MISSING
    """New status. `disabled` revokes every session of the account; `active` is always allowed."""


class UserAccountListItem(ApiModel):
    """A user account as one entry of the list."""

    id: UUID
    """ID of the user account; the tokens of the person carry it as `principal_id`."""
    party_id: UUID
    """ID of the person party the account belongs to."""
    email: str | None
    """Login e-mail address, lowercased; `null` when the account has none."""
    status: UserAccountStatus
    """`active` from its creation; a `disabled` account cannot log in."""
    has_password: bool
    """Whether a password is set; an account without one cannot log in with a password yet."""
    roles: list[Role]
    """Every role the account holds, stored (`admin`) and derived from the CRM, sorted."""
    last_login_at: datetime | None
    """When the person last logged in; `null` until the first login."""
    created_at: datetime
    """When the account was created."""

    @classmethod
    def from_view(cls, view: UserAccountWithRoles) -> Self:
        return cls(**_list_item_fields(view))


class UserAccountDetail(UserAccountListItem):
    """A user account with everything an administrator needs - never the hash or the login counter."""

    locked_until: datetime | None
    """Until when the password login is locked after failed attempts; `null` while it is not locked."""
    updated_at: datetime
    """When the account was last changed."""

    @classmethod
    def from_view(cls, view: UserAccountWithRoles) -> Self:
        return cls(
            **_list_item_fields(view), locked_until=view.account.locked_until, updated_at=view.account.updated_at
        )


def _list_item_fields(view: UserAccountWithRoles) -> dict[str, Any]:
    account = view.account
    return {
        "id": account.id,
        "party_id": account.party_id,
        "email": account.email,
        "status": account.status,
        "has_password": account.password_hash is not None,
        "roles": sorted(view.roles),
        "last_login_at": account.last_login_at,
        "created_at": account.created_at,
    }


class ActionTokenResponse(ApiModel):
    """A one-time token to hand to the person: an invitation or a password reset."""

    token: str
    """The token. SkillForge stores only its hash, so this is the one time it can be read."""
    expires_at: datetime
    """When the token stops working. Issuing another of the same purpose invalidates it earlier."""

    @classmethod
    def from_issued(cls, issued: IssuedActionToken) -> Self:
        return cls(token=issued.plaintext, expires_at=issued.token.expires_at)


class PasswordRedeemRequest(ApiModel):
    """Body of `POST /password/redeem`: a one-time token and the password to set with it."""

    token: str = Field(examples=["sf_ua_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"])
    """The invitation or password-reset token the person was given."""
    new_password: str = Field(examples=["correct horse battery staple"])
    """The password to set: 8 to 128 characters, no further rules."""


class RefreshTokenRevokeRequest(ApiModel):
    """Body of `POST /revoke`: the refresh token of the session to end."""

    refresh_token: str = Field(examples=["sf_rt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"])
    """A refresh token of the session - the current one or the one it replaced. A token of another client's
    session, or one SkillForge does not know, ends nothing and is answered the same."""


class DiscordLinkRequest(ApiModel):
    """Body of `PUT /discord-links/{discord_user_id}`."""

    party_id: UUID = Field(examples=["7d9f4f3e-1c2b-4a5d-9e8f-0a1b2c3d4e5f"])
    """ID of the person party the Discord account speaks for. A company is refused; the party needs no user account."""


class DiscordLink(ApiModel):
    """Which Discord account speaks for which person party - identity, written by admins only."""

    discord_user_id: DiscordUserId
    """The Discord user's snowflake, as a decimal string."""
    party_id: UUID
    """ID of the person party the Discord account speaks for."""
    active: bool
    """`false` once unlinked. The row stays as history, and a `PUT` links it again."""
    created_at: datetime
    """When the Discord account was first linked; a move to another party keeps it."""
    updated_at: datetime
    """When the link was last linked, moved or unlinked - what `updated_since` compares."""

    @classmethod
    def from_model(cls, link: DiscordAccount) -> Self:
        return cls(
            discord_user_id=link.discord_id,
            party_id=link.party_id,
            active=link.active,
            created_at=link.created_at,
            updated_at=link.updated_at,
        )
