import json
import logging
from typing import Annotated, Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Response
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.api.v1.common import register_exception_handlers
from app.core.auth import (
    AuthMethod,
    AuthSettings,
    Principal,
    UserPrincipal,
    create_access_token,
    create_application_access_token,
    require_scopes,
)
from app.core.auth.dependencies import get_auth_settings
from app.core.logging import LogFormat, LoggingSettings, LogLevel, configure_logging, register_request_logging
from app.core.logging.middleware import _logged_path
from app.main import app as real_app

BotWritePrincipal = Annotated[Principal, require_scopes("bot:write")]
AccountSelfPrincipal = Annotated[Principal, require_scopes("account:self")]


def test_logging_settings_default_to_skillforge_app_name():
    settings = LoggingSettings()

    assert settings.app_name == "skillforge"
    assert settings.file_path.name == "skillforge.jsonl"


async def test_request_logging_logs_not_found_with_request_id(capsys):
    configure_logging(LoggingSettings(level=LogLevel.WARNING, format=LogFormat.JSON))
    app = FastAPI()
    register_request_logging(app)
    capsys.readouterr()

    response = await _request(app, "GET", "/missing")

    output = capsys.readouterr().out
    event = json.loads(output)

    assert response.status_code == 404
    assert response.headers["x-request-id"] == event["request_id"]
    assert event["event"] == "http_request_not_found"
    assert event["level"] == "warning"
    assert event["method"] == "GET"
    assert event["path"] == "/missing"
    assert event["status_code"] == 404


async def test_request_logging_includes_auth_context_for_missing_scopes(capsys):
    configure_logging(LoggingSettings(level=LogLevel.WARNING, format=LogFormat.JSON))
    app = FastAPI()
    app.dependency_overrides[get_auth_settings] = _settings
    register_request_logging(app)

    @app.post("/write")
    async def write(principal: BotWritePrincipal):
        return {"client_id": principal.client_id}

    token = create_application_access_token(
        _settings(),
        principal_id=uuid4(),
        client_id="skillbot",
        scopes=["bot:read"],
    )
    capsys.readouterr()

    response = await _request(app, "POST", "/write", headers={"Authorization": f"Bearer {token.access_token}"})

    output = capsys.readouterr().out
    event = json.loads(output)

    assert response.status_code == 403
    assert event["event"] == "http_request_forbidden"
    assert event["auth_reason"] == "missing_scopes"
    assert event["client_id"] == "skillbot"
    assert event["required_scopes"] == ["bot:write"]
    assert event["missing_scopes"] == ["bot:write"]


async def test_request_logging_identifies_the_person_behind_a_request(capsys):
    """A person's token says who called - their account and party - on a granted and a forbidden request
    alike, but never the session."""
    configure_logging(LoggingSettings(level=LogLevel.INFO, format=LogFormat.JSON))
    app = FastAPI()
    app.dependency_overrides[get_auth_settings] = _settings
    register_request_logging(app)

    @app.get("/account")
    async def account(principal: AccountSelfPrincipal):
        return {"client_id": principal.client_id}

    @app.post("/write")
    async def write(principal: BotWritePrincipal):
        return {"client_id": principal.client_id}

    user_id, party_id, session_id = uuid4(), uuid4(), uuid4()
    person = UserPrincipal(
        principal_id=user_id,
        client_id="portal",
        scopes=frozenset({"account:self"}),
        party_id=party_id,
        session_id=session_id,
        roles=frozenset(),
        auth_methods=frozenset({AuthMethod.PASSWORD}),
    )
    headers = {"Authorization": f"Bearer {create_access_token(_settings(), person).access_token}"}
    capsys.readouterr()

    granted = await _request(app, "GET", "/account", headers=headers)
    forbidden = await _request(app, "POST", "/write", headers=headers)

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    requests = [event for event in events if event["event"].startswith("http_request_")]
    assert (granted.status_code, forbidden.status_code) == (200, 403)
    assert [event["event"] for event in requests] == ["http_request_completed", "http_request_forbidden"]
    for event in requests:
        assert event["principal_type"] == "user"
        assert event["user_id"] == str(user_id)
        assert event["party_id"] == str(party_id)
        assert str(session_id) not in json.dumps(event)


async def test_request_logging_redacts_a_discord_user_id_from_the_path(capsys):
    """A Discord user ID is identity data: it appears in audit rows only, never in the request log
    (bot-decoupling spec, "Security rules")."""
    configure_logging(LoggingSettings(level=LogLevel.WARNING, format=LogFormat.JSON))
    capsys.readouterr()

    response = await _request(real_app, "GET", "/api/v1/auth/discord-links/123456789012345678")

    output = capsys.readouterr().out
    event = json.loads(output)

    assert response.status_code == 401
    assert event["path"] == "/api/v1/auth/discord-links/{discord_user_id}"
    assert "123456789012345678" not in json.dumps(event)


async def test_request_logging_keeps_an_ordinary_routes_raw_path(capsys):
    """The redaction is scoped to the named path parameters: an ordinary route still logs its concrete path."""
    configure_logging(LoggingSettings(level=LogLevel.WARNING, format=LogFormat.JSON))
    capsys.readouterr()

    response = await _request(real_app, "GET", "/api/v1/auth/me")

    output = capsys.readouterr().out
    event = json.loads(output)

    assert response.status_code == 401
    assert event["path"] == "/api/v1/auth/me"


async def test_request_logging_redacts_a_discord_user_id_behind_a_trailing_slash_redirect(capsys):
    """`redirect_slashes` matches against a copy of the scope, so routing never populates `path_params` for the
    redirect response - the redaction must not depend on routing state to still catch the snowflake here."""
    configure_logging(LoggingSettings(level=LogLevel.INFO, format=LogFormat.JSON))
    capsys.readouterr()

    response = await _request(real_app, "GET", "/api/v1/auth/discord-links/123456789012345678/")

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    requests = [event for event in events if event["event"].startswith("http_request_")]

    assert response.status_code == 307
    assert len(requests) == 1
    assert "123456789012345678" not in json.dumps(requests[0])


async def test_request_logging_redacts_a_discord_user_id_on_an_unmatched_sub_path(capsys):
    """A 404 sub-path beneath the Discord user ID never reaches routing either - same requirement as above."""
    configure_logging(LoggingSettings(level=LogLevel.WARNING, format=LogFormat.JSON))
    capsys.readouterr()

    response = await _request(real_app, "GET", "/api/v1/auth/discord-links/123456789012345678/x")

    output = capsys.readouterr().out
    event = json.loads(output)

    assert response.status_code == 404
    assert "123456789012345678" not in json.dumps(event)


async def test_request_logging_redacts_a_short_numeric_discord_user_id_without_touching_other_segments(capsys):
    """A substring replacement would also turn `/v1/` into `/v{discord_user_id}/`; the segment-based
    redaction must leave every segment but the one right after `discord-links` alone."""
    configure_logging(LoggingSettings(level=LogLevel.WARNING, format=LogFormat.JSON))
    capsys.readouterr()

    response = await _request(real_app, "GET", "/api/v1/auth/discord-links/1")

    output = capsys.readouterr().out
    event = json.loads(output)

    assert response.status_code == 401
    assert event["path"] == "/api/v1/auth/discord-links/{discord_user_id}"


async def test_request_logging_redacts_a_discord_user_id_behind_a_doubled_slash(capsys):
    """An empty segment between `discord-links` and the ID must not break the adjacency check - the ID still
    needs redacting whether or not routing ever sees it as a neighbour of `discord-links`."""
    configure_logging(LoggingSettings(level=LogLevel.WARNING, format=LogFormat.JSON))
    capsys.readouterr()

    response = await _request(real_app, "GET", "/api/v1/auth/discord-links//123456789012345678")

    output = capsys.readouterr().out
    event = json.loads(output)

    assert response.status_code == 404
    assert event["path"] == "/api/v1/auth/discord-links//{discord_user_id}"
    assert "123456789012345678" not in json.dumps(event)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/discord-links", "/discord-links"),
        ("/discord-links/", "/discord-links/"),
        ("/discord-links/123456789012345678", "/discord-links/{discord_user_id}"),
        ("/discord-links/123456789012345678/", "/discord-links/{discord_user_id}/"),
        ("/discord-links/123456789012345678/x", "/discord-links/{discord_user_id}/x"),
        ("/discord-links//123456789012345678", "/discord-links//{discord_user_id}"),
        ("/discord-links///123456789012345678", "/discord-links///{discord_user_id}"),
        ("/api/v1/auth/discord-links/1", "/api/v1/auth/discord-links/{discord_user_id}"),
        ("/api/v1/auth/discord-links/redeem", "/api/v1/auth/discord-links/redeem"),
        ("/api/v1/auth/discord-links/-1", "/api/v1/auth/discord-links/{discord_user_id}"),
        ("/api/v1/auth/me", "/api/v1/auth/me"),
        ("/", "/"),
        ("", ""),
    ],
)
def test_logged_path_redacts_the_segment_after_a_redacted_key(path: str, expected: str):
    assert _logged_path(path) == expected


async def test_request_logging_stays_silent_for_healthy_probe(capsys):
    configure_logging(LoggingSettings(level=LogLevel.WARNING, format=LogFormat.JSON))
    app = FastAPI()
    register_request_logging(app)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    capsys.readouterr()

    response = await _request(app, "GET", "/health")

    assert response.status_code == 200
    assert capsys.readouterr().out == ""


async def test_request_logging_warns_on_unhealthy_probe(capsys):
    configure_logging(LoggingSettings(level=LogLevel.WARNING, format=LogFormat.JSON))
    app = FastAPI()
    register_request_logging(app)

    @app.get("/health")
    async def health(response: Response):
        response.status_code = 503
        return {"status": "unhealthy"}

    capsys.readouterr()

    response = await _request(app, "GET", "/health")

    event = json.loads(capsys.readouterr().out)

    assert response.status_code == 503
    assert event["event"] == "http_probe_unhealthy"
    assert event["level"] == "warning"
    assert event["status_code"] == 503


async def test_request_logging_still_errors_on_non_probe_5xx(capsys):
    configure_logging(LoggingSettings(level=LogLevel.WARNING, format=LogFormat.JSON))
    app = FastAPI()
    register_request_logging(app)

    @app.get("/boom")
    async def boom(response: Response):
        response.status_code = 500
        return {"detail": "nope"}

    capsys.readouterr()

    response = await _request(app, "GET", "/boom")

    event = json.loads(capsys.readouterr().out)

    assert response.status_code == 500
    assert event["event"] == "http_request_failed"
    assert event["level"] == "error"


async def test_request_logging_keeps_the_traceback_when_the_500_envelope_handles_the_exception(capsys):
    configure_logging(LoggingSettings(level=LogLevel.WARNING, format=LogFormat.JSON))
    app = FastAPI()
    register_request_logging(app)
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaputt")

    capsys.readouterr()

    response = await _request(app, "GET", "/boom", raise_app_exceptions=False)

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    failed = [event for event in events if event["event"] == "http_request_failed"]

    assert response.json()["code"] == "internal_error"
    assert len(failed) == 1
    assert failed[0]["level"] == "error"
    assert failed[0]["status_code"] == 500
    assert "RuntimeError" in json.dumps(failed[0])
    assert "kaputt" in json.dumps(failed[0])


async def test_500_envelope_carries_the_request_id_of_the_logged_failure(capsys):
    configure_logging(LoggingSettings(level=LogLevel.WARNING, format=LogFormat.JSON))
    app = _failing_app()
    capsys.readouterr()

    response = await _request(app, "GET", "/boom", raise_app_exceptions=False)

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    failed = next(event for event in events if event["event"] == "http_request_failed")
    assert response.status_code == 500
    assert response.headers["x-request-id"] == failed["request_id"]


async def test_500_envelope_echoes_a_request_id_the_client_sent():
    configure_logging(LoggingSettings(level=LogLevel.WARNING, format=LogFormat.JSON))

    response = await _request(
        _failing_app(), "GET", "/boom", raise_app_exceptions=False, headers={"x-request-id": "trace-me-42"}
    )

    assert response.headers["x-request-id"] == "trace-me-42"


async def _request(
    app: FastAPI, method: str, path: str, *, raise_app_exceptions: bool = True, **kwargs: Any
) -> httpx.Response:
    transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


def _failing_app() -> FastAPI:
    app = FastAPI()
    register_request_logging(app)
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaputt")

    return app


def test_configure_logging_disables_uvicorn_access_log():
    configure_logging(LoggingSettings(level=LogLevel.INFO, format=LogFormat.JSON))

    assert logging.getLogger("uvicorn").level == logging.NOTSET
    assert logging.getLogger("uvicorn.error").level == logging.NOTSET
    assert logging.getLogger("uvicorn.access").disabled is True


def _settings() -> AuthSettings:
    return AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
