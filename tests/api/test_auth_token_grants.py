"""The token endpoint's person grants without a database: parsing the form, dispatching on the grant, and turning
a denial into its OAuth2 answer (user-authentication spec, P0-8; the Discord-user grant: bot-decoupling spec, P0-7)."""

import inspect
import re
from typing import Any

import httpx
import pytest

from app.api.v1.auth.token import create_token, get_exchange_discord_user, get_issue_user_token, get_refresh_user_token
from app.api.v1.common import DBSession
from app.core.auth import Scope
from app.core.auth.inputs import MAX_BIGINT
from app.core.auth.scopes import CLIENT_ONLY_SCOPES
from app.core.db.dependencies import get_db_session
from app.core.logging import LogFormat, LoggingSettings, LogLevel, configure_logging
from app.main import app
from app.services.auth import IssuedUserToken, TokenDenial
from tests.api.test_auth_endpoint import _overrides, _token
from tests.api.test_auth_endpoint import _post as _post_to

ISSUED = IssuedUserToken(
    token=_token(scope="account:self crm:read:own"), refresh_token="sf_rt_refresh", refresh_expires_in=2592000
)
DISCORD_USER_GRANT = "urn:skillforge:params:oauth:grant-type:discord-user"
SNOWFLAKE = 123456789012345678
"""A Discord user ID above 2**53: a float or a JavaScript number would round it."""
EXCHANGED = _token(scope="crm:read:own")


class _Fakes(_overrides):
    """``_overrides`` with all four grant seams faked: each records its keyword arguments and answers ``result``."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []
        super().__init__(self._fake("client_credentials"))

    def _fake(self, name: str):
        async def fake(session, settings, **kwargs):
            self.calls.append((name, kwargs))
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

        return fake

    def __enter__(self) -> _Fakes:
        super().__enter__()
        app.dependency_overrides[get_issue_user_token] = lambda: self._fake("password")
        app.dependency_overrides[get_refresh_user_token] = lambda: self._fake("refresh_token")
        app.dependency_overrides[get_exchange_discord_user] = lambda: self._fake("discord_user")
        return self


async def _post(data: dict[str, str], **kwargs: Any) -> httpx.Response:
    return await _post_to("/api/v1/auth/token", data=data, **kwargs)


def _exchange_form(discord_user_id: str | None = str(SNOWFLAKE), **fields: str) -> dict[str, str]:
    """The form of the Discord-user grant; ``discord_user_id=None`` leaves the field out."""
    form = {"grant_type": DISCORD_USER_GRANT, **fields}
    return form if discord_user_id is None else form | {"discord_user_id": discord_user_id}


@pytest.fixture
def restore_logging():
    """Put the logging configuration back, so the turned-up level ends with the test."""
    yield
    configure_logging(LoggingSettings())


async def test_the_password_grant_calls_its_seam_and_answers_with_the_refresh_token():
    with _Fakes(ISSUED) as fakes:
        response = await _post(
            {"grant_type": "password", "username": "anna@example.org", "password": "pw", "scope": "account:self"},
            auth=("portal", "secret"),
        )

    assert response.status_code == 200
    assert response.json() == {
        "access_token": "encoded-token",
        "token_type": "bearer",
        "expires_in": 900,
        "scope": "account:self crm:read:own",
        "refresh_token": "sf_rt_refresh",
        "refresh_expires_in": 2592000,
    }
    assert fakes.calls == [
        (
            "password",
            {
                "client_id": "portal",
                "client_secret": "secret",
                "username": "anna@example.org",
                "password": "pw",
                "requested_scopes": "account:self",
            },
        )
    ]


async def test_the_refresh_token_grant_calls_its_seam():
    with _Fakes(ISSUED) as fakes:
        response = await _post({
            "grant_type": "refresh_token",
            "refresh_token": "sf_rt_old",
            "client_id": "portal",
            "client_secret": "s",
        })

    assert response.status_code == 200
    assert fakes.calls == [
        (
            "refresh_token",
            {"client_id": "portal", "client_secret": "s", "refresh_token": "sf_rt_old", "requested_scopes": None},
        )
    ]


@pytest.mark.parametrize(
    ("denial", "status", "body"),
    [
        (TokenDenial.INVALID_CLIENT, 401, {"detail": "Invalid client credentials", "code": "invalid_client"}),
        (
            TokenDenial.UNAUTHORIZED_CLIENT,
            400,
            {"detail": "Client may not use this grant", "code": "unauthorized_client"},
        ),
        (TokenDenial.INVALID_GRANT, 400, {"detail": "Invalid credentials or refresh token", "code": "invalid_grant"}),
        (TokenDenial.INVALID_SCOPE, 400, {"detail": "Invalid requested scope", "code": "invalid_scope"}),
    ],
)
@pytest.mark.parametrize(
    "form",
    [
        {"grant_type": "password", "username": "anna@example.org", "password": "pw"},
        {"grant_type": "refresh_token", "refresh_token": "sf_rt_old"},
        {"grant_type": DISCORD_USER_GRANT, "discord_user_id": str(SNOWFLAKE)},
    ],
)
async def test_every_denial_is_answered_with_its_oauth2_error(
    denial: TokenDenial, status: int, body: dict[str, str], form: dict[str, str]
):
    with _Fakes(denial):
        response = await _post(form, auth=("portal", "secret"))

    assert (response.status_code, response.json()) == (status, body)


async def test_a_denial_commits_the_session_so_what_it_wrote_survives():
    outcome: list[str] = []

    async def tracked_session():
        try:
            yield "session"
        except BaseException:
            outcome.append("rolled back")
            raise
        outcome.append("committed")

    with _Fakes(TokenDenial.INVALID_GRANT):
        app.dependency_overrides[get_db_session] = tracked_session
        response = await _post(
            {"grant_type": "password", "username": "a@example.org", "password": "pw"}, auth=("p", "s")
        )

    assert response.status_code == 400
    assert outcome == ["committed"]


@pytest.mark.parametrize(
    "form",
    [
        {"grant_type": "password", "password": "pw"},
        {"grant_type": "password", "username": "anna@example.org"},
        {"grant_type": "password", "username": "", "password": "pw"},
        {"grant_type": "refresh_token"},
        {"grant_type": "refresh_token", "refresh_token": ""},
    ],
)
async def test_a_parameter_the_grant_needs_and_lacks_is_invalid_request(form: dict[str, str]):
    with _Fakes(AssertionError("no seam is called")) as fakes:
        response = await _post(form, auth=("portal", "secret"))

    assert response.status_code == 422
    assert response.json() == {"detail": "A required parameter is missing", "code": "invalid_request"}
    assert fakes.calls == []


@pytest.mark.parametrize(
    ("username", "password", "accepted"),
    [
        ("a" * 242 + "@example.org", "p" * 128, True),
        ("a" * 243 + "@example.org", "pw", False),
        ("anna@example.org", "p" * 129, False),
    ],
)
async def test_an_over_long_username_or_password_is_invalid_request_before_any_look_up(
    username: str, password: str, accepted: bool
):
    with _Fakes(ISSUED) as fakes:
        response = await _post(
            {"grant_type": "password", "username": username, "password": password}, auth=("portal", "secret")
        )

    assert (response.status_code == 200) is accepted
    assert len(fakes.calls) == int(accepted)
    if not accepted:
        assert response.json() == {"detail": "A required parameter is missing", "code": "invalid_request"}


async def test_the_discord_user_grant_calls_its_seam_and_answers_without_a_refresh_token():
    with _Fakes(EXCHANGED) as fakes:
        response = await _post(_exchange_form(scope="crm:read:own"), auth=("bot", "secret"))

    assert response.status_code == 200
    assert response.json() == {
        "access_token": "encoded-token",
        "token_type": "bearer",
        "expires_in": 900,
        "scope": "crm:read:own",
    }
    assert fakes.calls == [
        (
            "discord_user",
            {
                "client_id": "bot",
                "client_secret": "secret",
                "discord_user_id": SNOWFLAKE,
                "requested_scopes": "crm:read:own",
            },
        )
    ]


@pytest.mark.parametrize("discord_user_id", ["0", str(MAX_BIGINT)])
async def test_the_smallest_and_the_largest_discord_user_id_reach_the_seam_as_ints(discord_user_id: str):
    with _Fakes(EXCHANGED) as fakes:
        response = await _post(_exchange_form(discord_user_id), auth=("bot", "secret"))

    assert response.status_code == 200
    assert [kwargs["discord_user_id"] for _, kwargs in fakes.calls] == [int(discord_user_id)]


@pytest.mark.parametrize(
    "discord_user_id",
    [None, "", "-1", "+5", " 7", "1e3", "0x10", "12.0", "\uff11\uff12", str(MAX_BIGINT + 1), "0" * 20],
)
async def test_a_missing_or_malformed_discord_user_id_is_invalid_request_before_any_look_up(
    discord_user_id: str | None,
):
    with _Fakes(AssertionError("no seam is called")) as fakes:
        response = await _post(_exchange_form(discord_user_id), auth=("bot", "secret"))

    assert response.status_code == 422
    assert response.json() == {"detail": "A required parameter is missing", "code": "invalid_request"}
    assert fakes.calls == []


async def test_the_request_log_names_no_discord_user_id(restore_logging, capsys):
    """Discord user IDs appear in audit rows only, never in the request log (bot-decoupling spec, "Security rules")."""
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()

    with _Fakes(TokenDenial.INVALID_GRANT):
        denied = await _post(_exchange_form(), auth=("bot", "secret"))
        malformed = await _post(_exchange_form(f"{SNOWFLAKE}x"), auth=("bot", "secret"))

    output = capsys.readouterr().out
    assert (denied.status_code, malformed.status_code) == (400, 422)
    assert output.count("http_request_") >= 2, "the requests were logged"
    assert str(SNOWFLAKE) not in output


def test_the_token_form_and_the_operation_document_the_discord_user_grant():
    schema = app.openapi()
    form = schema["components"]["schemas"]["Body_auth_create_token"]["properties"]
    description = schema["paths"]["/api/v1/auth/token"]["post"]["description"]

    assert DISCORD_USER_GRANT in form["grant_type"]["description"]
    assert [variant["type"] for variant in form["discord_user_id"]["anyOf"]] == ["string", "null"]
    for phrase in (DISCORD_USER_GRANT, "auth:users:exchange", "No refresh token", "try it with curl"):
        assert phrase in description, phrase


def test_the_exchange_scope_is_offered_to_clients_only():
    [scheme] = app.openapi()["components"]["securitySchemes"].values()

    assert "auth:users:exchange" in scheme["flows"]["clientCredentials"]["scopes"]
    assert "auth:users:exchange" not in scheme["flows"]["password"]["scopes"]


def test_swagger_uis_authorize_dialog_offers_the_password_flow():
    [scheme] = app.openapi()["components"]["securitySchemes"].values()

    password = scheme["flows"]["password"]
    assert password["tokenUrl"] == "/api/v1/auth/token"
    assert password["scopes"] == {scope.value: scope.description for scope in Scope if scope not in CLIENT_ONLY_SCOPES}
    assert scheme["flows"]["clientCredentials"]["scopes"] == {scope.value: scope.description for scope in Scope}


def test_the_token_response_and_form_describe_every_property():
    schemas = app.openapi()["components"]["schemas"]

    for name in ("AccessTokenResponse", "Body_auth_create_token", "RefreshTokenRevokeRequest"):
        properties = schemas[name]["properties"]
        assert properties, name
        assert all(prop.get("description") for prop in properties.values()), name
    assert schemas["AccessTokenResponse"]["required"] == ["access_token", "token_type", "expires_in", "scope"]
    assert schemas["Body_auth_create_token"]["required"] == ["grant_type"]


def test_the_revoke_route_is_a_login_clients_route_and_documents_no_body_on_success():
    operation = app.openapi()["paths"]["/api/v1/auth/revoke"]["post"]

    assert operation["operationId"] == "auth_revoke_refresh_token"
    assert operation["security"] == [{"OAuth2": ["auth:users:login"]}]
    assert set(operation["responses"]) == {"204", "401", "403", "422"}


def test_every_auth_operation_id_is_auth_and_the_function_name():
    ids = [
        operation["operationId"]
        for path, item in app.openapi()["paths"].items()
        if path.startswith("/api/v1/auth/")
        for operation in item.values()
    ]

    assert ids
    assert all(re.fullmatch(r"auth_[a-z_]+", operation_id) for operation_id in ids), ids


def test_the_auth_tag_describes_person_logins():
    [auth] = [tag for tag in app.openapi()["tags"] if tag["name"] == "auth"]

    assert "password" in auth["description"]
    assert "refresh" in auth["description"]


def test_create_token_shares_the_function_scoped_request_session():
    """``DBSession`` commits before the response is sent: a denial's audit entry is durable once it is answered."""
    assert inspect.signature(create_token).parameters["session"].annotation == DBSession
