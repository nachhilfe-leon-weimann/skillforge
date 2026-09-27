"""Access tokens of both principal types: what `create_access_token` writes and what validation accepts back.

The application token is pinned here too: its claims are the contract SkillBot already lives on, so
this file asserts them against a fixture that a new claim would break.
"""

import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pytest
from pydantic import SecretStr

from app.core.auth import (
    ApplicationPrincipal,
    AuthSettings,
    CreatedAccessToken,
    DiscordLogin,
    Login,
    PasswordLogin,
    TokenValidationError,
    UserPrincipal,
    create_access_token,
    create_application_access_token,
    validate_access_token,
)
from app.core.auth.roles import Role

ISSUED_AT = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
EXPIRES_AT = ISSUED_AT + timedelta(minutes=15)
USER_ID = UUID("aaaaaaaa-1111-1111-1111-111111111111")
PARTY_ID = UUID("22222222-2222-2222-2222-222222222222")
SESSION_ID = UUID("33333333-3333-3333-3333-333333333333")
APPLICATION_ID = UUID("44444444-4444-4444-4444-444444444444")
DISCORD = {"amr": ["discord"], "scope": "crm:read:own"}
"""The claims that make ``_encode_person_claims`` a Discord token - together with ``without="sid"``."""


def test_application_access_token_claims_match_the_fixture():
    """The shape SkillBot's tokens have today. A new claim on an application token breaks this."""
    settings = _settings()

    created = create_application_access_token(
        settings,
        principal_id=APPLICATION_ID,
        client_id="integration",
        scopes=["auth:users:manage", "auth:clients:manage"],
        now=ISSUED_AT,
    )

    claims = _decode(created.access_token, settings)
    assert UUID(str(claims.pop("jti")))
    assert claims == {
        "iss": "skillforge",
        "aud": "skillforge-api",
        "sub": "app:integration",
        "principal_type": "application",
        "principal_id": str(APPLICATION_ID),
        "azp": "integration",
        "scope": "auth:clients:manage auth:users:manage",
        "iat": int(ISSUED_AT.timestamp()),
        "exp": int(EXPIRES_AT.timestamp()),
    }


def test_an_application_token_validates_back_into_the_application_principal_it_was_issued_for():
    settings = _settings()
    created = create_application_access_token(
        settings,
        principal_id=APPLICATION_ID,
        client_id="integration",
        scopes=["auth:clients:manage"],
    )

    principal = validate_access_token(created.access_token, settings)

    assert principal == ApplicationPrincipal(
        principal_id=APPLICATION_ID, client_id="integration", scopes=frozenset({"auth:clients:manage"})
    )
    assert principal.subject == "app:integration"


def test_person_access_token_claims_match_the_fixture():
    settings = _settings()

    created = _person_token(
        settings, scopes={"crm:read:own", "account:self"}, roles={Role.TUTOR, Role.ADMIN}, now=ISSUED_AT
    )

    claims = _decode(created.access_token, settings)
    assert UUID(str(claims.pop("jti")))
    assert claims == {
        "iss": "skillforge",
        "aud": "skillforge-api",
        "sub": f"user:{USER_ID}",
        "principal_type": "user",
        "principal_id": str(USER_ID),
        "azp": "portal",
        "scope": "account:self crm:read:own",
        "party_id": str(PARTY_ID),
        "sid": str(SESSION_ID),
        "roles": ["admin", "tutor"],
        "amr": ["pwd"],
        "iat": int(ISSUED_AT.timestamp()),
        "exp": int(EXPIRES_AT.timestamp()),
    }


def test_a_password_token_is_byte_for_byte_what_it_was_before_the_discord_login():
    """The payload bytes - claim order included - of a password or refresh token before P0-7 of bot-decoupling.md
    added the Discord login; only ``jti`` differs from token to token."""
    settings = _settings()

    created = _person_token(
        settings, scopes={"crm:read:own", "account:self"}, roles={Role.TUTOR, Role.ADMIN}, now=ISSUED_AT
    )

    segment = created.access_token.split(".")[1]
    payload = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    jti = json.loads(payload)["jti"]
    expected = (
        '{"iss":"skillforge","aud":"skillforge-api",'
        f'"sub":"user:{USER_ID}","principal_id":"{USER_ID}","azp":"portal","scope":"account:self crm:read:own",'
        f'"principal_type":"user","party_id":"{PARTY_ID}","sid":"{SESSION_ID}","roles":["admin","tutor"],'
        f'"amr":["pwd"],"iat":{int(ISSUED_AT.timestamp())},"exp":{int(EXPIRES_AT.timestamp())},"jti":"{jti}"}}'
    )
    assert payload == expected.encode()


def test_a_discord_token_carries_amr_discord_and_no_sid():
    settings = _settings()

    created = _person_token(settings, scopes={"crm:read:own"}, login=DiscordLogin(), now=ISSUED_AT)

    claims = _decode(created.access_token, settings)
    assert UUID(str(claims.pop("jti")))
    assert claims == {
        "iss": "skillforge",
        "aud": "skillforge-api",
        "sub": f"user:{USER_ID}",
        "principal_type": "user",
        "principal_id": str(USER_ID),
        "azp": "portal",
        "scope": "crm:read:own",
        "party_id": str(PARTY_ID),
        "roles": [],
        "amr": ["discord"],
        "iat": int(ISSUED_AT.timestamp()),
        "exp": int(EXPIRES_AT.timestamp()),
    }


def test_a_person_token_carries_the_canonical_scope():
    """A token never holds both a scope and its `:own` variant (ADR 0008)."""
    settings = _settings()

    created = _person_token(settings, scopes={"crm:read", "crm:read:own", "account:self"})

    assert created.scope == "account:self crm:read"
    assert _decode(created.access_token, settings)["scope"] == "account:self crm:read"


def test_a_person_token_validates_back_into_the_user_principal_it_was_issued_for():
    settings = _settings()
    person = _person(scopes={"account:self", "crm:read:own"}, roles={Role.ADMIN, Role.GUARDIAN})

    principal = validate_access_token(create_access_token(settings, person).access_token, settings)

    assert principal == person
    assert isinstance(principal, UserPrincipal)
    assert principal.party_id == PARTY_ID
    assert principal.roles == frozenset({Role.ADMIN, Role.GUARDIAN})
    assert principal.login == PasswordLogin(session_id=SESSION_ID)
    assert principal.subject == f"user:{USER_ID}"


def test_a_discord_token_validates_back_into_a_discord_login():
    settings = _settings()
    person = _person(scopes={"crm:read", "crm:write"}, login=DiscordLogin())

    principal = validate_access_token(create_access_token(settings, person).access_token, settings)

    assert principal == person
    assert isinstance(principal, UserPrincipal)
    assert principal.login == DiscordLogin()


def test_the_claims_the_rejections_below_start_from_are_valid():
    """Each rejection changes one claim of this token, so it is the change that makes the token invalid."""
    settings = _settings()

    principal = validate_access_token(_encode_person_claims(settings), settings)
    discord = validate_access_token(_encode_person_claims(settings, without="sid", **DISCORD), settings)

    assert principal == _person(scopes={"account:self"})
    assert discord == _person(scopes={"crm:read:own"}, login=DiscordLogin())


@pytest.mark.parametrize("claim", ["party_id", "sid", "amr"])
def test_validate_access_token_rejects_a_person_token_without_a_required_claim(claim: str):
    settings = _settings()
    token = _encode_person_claims(settings, without=claim)

    with pytest.raises(TokenValidationError):
        validate_access_token(token, settings)


@pytest.mark.parametrize("roles", ["admin", ["pope"], [""], [1]])
def test_validate_access_token_rejects_a_person_token_with_an_unknown_role(roles: object):
    settings = _settings()
    token = _encode_person_claims(settings, roles=roles)

    with pytest.raises(TokenValidationError):
        validate_access_token(token, settings)


@pytest.mark.parametrize("amr", ["pwd", [], ["otp"], ["PWD"], [1]])
def test_validate_access_token_rejects_a_person_token_with_an_unknown_or_no_method(amr: object):
    settings = _settings()
    token = _encode_person_claims(settings, amr=amr)

    with pytest.raises(TokenValidationError):
        validate_access_token(token, settings)


@pytest.mark.parametrize(
    ("without", "changes"),
    [
        pytest.param("sid", {}, id="pwd-without-sid"),
        pytest.param(None, {"sid": None}, id="pwd-with-null-sid"),
        pytest.param(None, DISCORD, id="discord-with-sid"),
        pytest.param(None, DISCORD | {"sid": None}, id="discord-with-null-sid"),
        pytest.param(None, {"amr": ["discord", "pwd"]}, id="mixed-with-sid"),
        pytest.param("sid", {"amr": ["discord", "pwd"]}, id="mixed-without-sid"),
        pytest.param("sid", DISCORD | {"scope": "account:self"}, id="discord-with-account-self"),
        pytest.param("sid", DISCORD | {"scope": "crm:read:own auth:users:manage"}, id="discord-with-an-auth-scope"),
    ],
)
def test_validate_access_token_rejects_a_person_token_whose_amr_and_sid_name_no_login(
    without: str | None, changes: dict[str, object]
):
    """``[pwd]`` needs a session, ``[discord]`` has none and carries only ``VOUCHED_SCOPES``; nothing else is valid."""
    settings = _settings()
    token = _encode_person_claims(settings, without=without, **changes)

    with pytest.raises(TokenValidationError):
        validate_access_token(token, settings)


@pytest.mark.parametrize(
    "subject",
    [
        f"user:{uuid4()}",
        f"user:{USER_ID.hex}",
        f"user:{str(USER_ID).upper()}",
        "app:portal",
        str(USER_ID),
    ],
)
def test_validate_access_token_rejects_a_person_token_whose_subject_is_not_its_principal(subject: str):
    """`sub` names the principal in the one spelling SkillForge writes: `user:<principal_id>`."""
    settings = _settings()
    token = _encode_person_claims(settings, sub=subject)

    with pytest.raises(TokenValidationError):
        validate_access_token(token, settings)


@pytest.mark.parametrize("claim", ["party_id", "sid", "principal_id"])
def test_validate_access_token_rejects_a_person_token_with_an_unusable_id(claim: str):
    settings = _settings()
    token = _encode_person_claims(settings, **{claim: "not-a-uuid"})

    with pytest.raises(TokenValidationError):
        validate_access_token(token, settings)


@pytest.mark.parametrize("scope", ["", "   ", 42, None, ["account:self"]])
def test_validate_access_token_rejects_a_person_token_without_a_usable_scope(scope: object):
    settings = _settings()
    token = _encode_person_claims(settings, scope=scope)

    with pytest.raises(TokenValidationError):
        validate_access_token(token, settings)


def test_validate_access_token_rejects_an_unknown_principal_type():
    settings = _settings()
    token = _encode_person_claims(settings, principal_type="service")

    with pytest.raises(TokenValidationError):
        validate_access_token(token, settings)


def test_validate_access_token_reads_the_scope_claim_in_its_canonical_form():
    settings = _settings()
    token = _encode_person_claims(settings, scope="crm:read crm:read:own")

    assert validate_access_token(token, settings).scopes == frozenset({"crm:read"})


def test_validate_access_token_rejects_a_person_token_with_a_client_only_scope():
    """Client-only scopes are what a client may do for itself; they never reach a person's token."""
    settings = _settings()
    token = _encode_person_claims(settings, scope="account:self auth:users:login")

    with pytest.raises(TokenValidationError):
        validate_access_token(token, settings)


def test_create_access_token_refuses_a_client_only_scope_for_a_person_only():
    settings = _settings()

    with pytest.raises(ValueError, match="client-only"):
        _person_token(settings, scopes={"account:self", "auth:users:login"})

    application = create_application_access_token(
        settings, principal_id=APPLICATION_ID, client_id="portal", scopes=["auth:users:login"]
    )
    assert validate_access_token(application.access_token, settings).scopes == frozenset({"auth:users:login"})


def test_create_access_token_rejects_empty_scopes():
    with pytest.raises(ValueError, match="scopes must not be empty"):
        _person_token(_settings(), scopes=set())


def test_create_access_token_refuses_a_scope_outside_the_vouched_ones_for_a_discord_login_only():
    """``account:self`` and every ``auth:*`` scope keep demanding a password login (decision R)."""
    settings = _settings()

    with pytest.raises(ValueError, match="vouched"):
        _person_token(settings, scopes={"crm:read:own", "account:self"}, login=DiscordLogin())

    password = _person_token(settings, scopes={"crm:read:own", "account:self"})
    assert validate_access_token(password.access_token, settings).scopes == {"crm:read:own", "account:self"}


def _person(
    *,
    scopes: set[str],
    roles: set[Role] | None = None,
    login: Login | None = None,
) -> UserPrincipal:
    return UserPrincipal(
        principal_id=USER_ID,
        client_id="portal",
        scopes=frozenset(scopes),
        party_id=PARTY_ID,
        roles=frozenset(roles or ()),
        login=PasswordLogin(session_id=SESSION_ID) if login is None else login,
    )


def _person_token(
    settings: AuthSettings,
    *,
    scopes: set[str],
    roles: set[Role] | None = None,
    login: Login | None = None,
    now: datetime | None = None,
) -> CreatedAccessToken:
    return create_access_token(settings, _person(scopes=scopes, roles=roles, login=login), now=now)


def _decode(token: str, settings: AuthSettings) -> dict[str, object]:
    """Decode without the expiry check: the fixtures pin a fixed issuing time."""
    return jwt.decode(
        token,
        settings.secret_key.get_secret_value(),
        algorithms=[settings.algorithm],
        issuer=settings.issuer,
        audience=settings.audience,
        options={"verify_exp": False},
    )


def _encode_person_claims(
    settings: AuthSettings,
    *,
    without: str | None = None,
    **overrides: object,
) -> str:
    """A correctly signed person's token with the claims SkillForge writes, but for ``overrides`` and ``without``."""
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": settings.issuer,
        "aud": settings.audience,
        "sub": f"user:{USER_ID}",
        "principal_type": "user",
        "principal_id": str(USER_ID),
        "azp": "portal",
        "scope": "account:self",
        "party_id": str(PARTY_ID),
        "sid": str(SESSION_ID),
        "roles": [],
        "amr": ["pwd"],
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "jti": str(uuid4()),
    }
    claims |= overrides
    claims.pop(without, None)

    return jwt.encode(claims, settings.secret_key.get_secret_value(), algorithm=settings.algorithm)


def _settings() -> AuthSettings:
    return AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
