"""The app token: a signed client assertion through the guarded transport, cached, and never shown anywhere."""

import base64
import traceback

import httpx
import jwt
import pytest
from pydantic import SecretStr

from app.core.logging import LogFormat, LoggingSettings, LogLevel, configure_logging
from app.integrations.config import IntegrationMode
from app.integrations.errors import (
    IntegrationError,
    IntegrationRefusedError,
    IntegrationRejectedError,
    UnexpectedResponseError,
)
from app.integrations.graph import credential as graph_credential
from app.integrations.graph.client import CALENDAR_VIEW_TEMPLATE, CALENDARS_TEMPLATE, GraphClient
from app.integrations.graph.config import GraphSettings
from app.integrations.graph.credential import TOKEN_PATH_TEMPLATE, TokenCache
from app.integrations.guards import CONTRACT_RUN_ENV
from tests.integrations.graph.fake_graph import (
    ACCESS_TOKEN,
    CALENDAR_ID,
    CALENDAR_NAME,
    CLIENT_ID,
    LOGIN_HOST,
    MAILBOX,
    TENANT_ID,
    TOKEN_REQUEST,
    VIEW_PATH,
    WINDOW,
    Credential,
    FakeGraph,
    answer,
    fixture,
    form,
)

TOKEN_URL = f"https://{LOGIN_HOST}{TOKEN_REQUEST['path']}"


async def connect_and_read(settings: GraphSettings, fake: FakeGraph, tokens: TokenCache) -> None:
    async with GraphClient.connect(settings, transport=fake.transport, tokens=tokens, sleep=fake.sleep) as client:
        await client.calendar_view(*WINDOW)


def token_requests(fake: FakeGraph) -> list[httpx.Request]:
    return [request for request in fake.requests if request.url.host == LOGIN_HOST]


async def test_the_token_request_is_the_documented_form_with_a_signed_assertion(
    settings, fake_graph: FakeGraph, tokens, credential: Credential
):
    await connect_and_read(settings, fake_graph, tokens)

    (request,) = token_requests(fake_graph)
    assert (request.method, str(request.url)) == ("POST", TOKEN_URL)
    assert request.headers["content-type"] == TOKEN_REQUEST["headers"]["content-type"]
    sent = form(request)
    assert {key: value for key, value in sent.items() if key != "client_assertion"} == {
        key: value for key, value in TOKEN_REQUEST["body"].items() if key != "client_assertion"
    }
    assertion = sent["client_assertion"]
    assert jwt.get_unverified_header(assertion) == {"alg": "RS256", "typ": "JWT", "x5t": credential.thumbprint}
    claims = jwt.decode(
        assertion, credential.key.public_key(), algorithms=["RS256"], audience=TOKEN_URL, issuer=CLIENT_ID
    )
    assert claims["sub"] == CLIENT_ID and claims["jti"]
    assert claims["nbf"] <= claims["iat"] < claims["exp"] <= claims["nbf"] + 600


async def test_every_assertion_is_new(settings, fake_graph: FakeGraph):
    await connect_and_read(settings, fake_graph, TokenCache())
    await connect_and_read(settings, fake_graph, TokenCache())

    first, second = (
        jwt.decode(form(request)["client_assertion"], options={"verify_signature": False})
        for request in token_requests(fake_graph)
    )
    assert first["jti"] != second["jti"]


async def test_the_token_is_sent_as_bearer_and_cached_until_shortly_before_it_expires(settings, fake_graph: FakeGraph):
    clock = [1000.0]
    tokens = TokenCache(clock=lambda: clock[0])
    expires_in = fixture("g7-token-response")["response"]["body"]["expires_in"]

    await connect_and_read(settings, fake_graph, tokens)
    clock[0] += expires_in - graph_credential.REFRESH_MARGIN_SECONDS - 1
    await connect_and_read(settings, fake_graph, tokens)
    assert len(token_requests(fake_graph)) == 1

    clock[0] += 1
    await connect_and_read(settings, fake_graph, tokens)
    assert len(token_requests(fake_graph)) == 2
    reads = [request for request in fake_graph.requests if request.url.host != LOGIN_HOST]
    assert {request.headers["Authorization"] for request in reads} == {f"Bearer {ACCESS_TOKEN}"}


async def test_the_token_request_goes_out_in_dry_run_as_a_read(settings, fake_graph: FakeGraph, tokens):
    assert settings.mode is IntegrationMode.DRY_RUN

    await connect_and_read(settings, fake_graph, tokens)

    assert len(token_requests(fake_graph)) == 1


async def test_live_mode_is_refused_under_pytest_even_in_the_contract_run(
    settings, fake_graph: FakeGraph, tokens, monkeypatch
):
    monkeypatch.setenv(CONTRACT_RUN_ENV, "1")

    with pytest.raises(IntegrationRefusedError, match="trial account only"):
        await connect_and_read(settings.model_copy(update={"mode": IntegrationMode.LIVE}), fake_graph, tokens)

    assert fake_graph.requests == []


async def test_a_refused_assertion_is_rejected_and_nothing_else_is_sent(settings, fake_graph: FakeGraph, tokens):
    fake_graph.on("POST", TOKEN_REQUEST["path"], answer("g7-token-error"))

    with pytest.raises(IntegrationRejectedError) as raised:
        await connect_and_read(settings, fake_graph, tokens)

    assert raised.value.status == 401
    assert raised.value.remote_message == "invalid_client (AADSTS700027)"
    assert len(fake_graph.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"token_type": "Bearer", "expires_in": 3599},
        {"token_type": "pop", "expires_in": 3599, "access_token": "synthetic"},
        {"token_type": "Bearer", "expires_in": 0, "access_token": "synthetic"},
        {"token_type": "Bearer", "expires_in": 3599, "access_token": "synthetic token"},
        {"token_type": "Bearer", "expires_in": 3599, "access_token": ""},
    ],
    ids=["no token", "another type", "already expired", "a space", "empty"],
)
async def test_an_answer_that_is_no_usable_token_is_unexpected(body, settings, fake_graph: FakeGraph, tokens):
    fake_graph.on("POST", TOKEN_REQUEST["path"], httpx.Response(200, json=body))

    with pytest.raises(UnexpectedResponseError) as raised:
        await connect_and_read(settings, fake_graph, tokens)

    assert "synthetic" not in str(raised.value)
    assert len(fake_graph.requests) == 1


def other_credential() -> Credential:
    return Credential.generate()


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"private_key_pem": SecretStr(other_credential().key_setting)}, "does not belong"),
        ({"certificate_pem": SecretStr(base64.b64encode(b"-----BEGIN CERTIFICATE-----\nAAAA\n").decode())}, "CERT"),
        ({"private_key_pem": SecretStr(base64.b64encode(b"-----BEGIN PRIVATE KEY-----\nAAAA\n").decode())}, "KEY"),
    ],
    ids=["a key of another certificate", "an unreadable certificate", "an unreadable key"],
)
async def test_a_credential_that_does_not_fit_sends_nothing(update, message, settings, fake_graph: FakeGraph, tokens):
    with pytest.raises(IntegrationRefusedError, match=message):
        await connect_and_read(settings.model_copy(update=update), fake_graph, tokens)

    assert fake_graph.requests == []


@pytest.fixture
def restore_logging():
    yield
    configure_logging(LoggingSettings())


async def test_certificate_key_assertion_and_token_appear_in_no_log_line_error_or_repr(
    settings, fake_graph: FakeGraph, credential: Credential, restore_logging, capsys, caplog
):
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()
    errors: list[Exception] = []
    seen: list[object] = []

    async def attempt(setup) -> None:
        fake = FakeGraph()
        setup(fake)
        try:
            async with GraphClient.connect(settings, transport=fake.transport, tokens=TokenCache()) as client:
                seen.append(client)
                await client.calendar_view(*WINDOW)
        except IntegrationError as exc:
            errors.append(exc)
        fake_graph.requests.extend(fake.requests)

    await attempt(lambda fake: None)
    await attempt(lambda fake: fake.on("POST", TOKEN_REQUEST["path"], answer("g7-token-error")))
    await attempt(lambda fake: fake.on("POST", TOKEN_REQUEST["path"], httpx.LocalProtocolError(f"b'{ACCESS_TOKEN}'")))
    await attempt(lambda fake: fake.on("GET", VIEW_PATH, answer("g6-mailbox-outside-scope")))
    await attempt(lambda fake: fake.on("GET", VIEW_PATH, httpx.Response(200, json={"value": [{"subject": "x"}]})))
    await attempt(lambda fake: fake.on("GET", VIEW_PATH, httpx.Response(429)))
    await attempt(lambda fake: fake.on("GET", f"/v1.0/users/{MAILBOX}/calendars", answer("g6-mailbox-outside-scope")))

    output = capsys.readouterr().out
    assert settings.certificate_pem is not None and settings.private_key_pem is not None
    assertions = [form(request)["client_assertion"] for request in fake_graph.requests if request.method == "POST"]
    assert len(errors) == 6 and len(assertions) == 7
    texts = [
        output,
        caplog.text,
        repr(settings),
        str(settings),
        *map(repr, seen),
        *(text for error in errors for text in (str(error), repr(error), "".join(traceback.format_exception(error)))),
    ]
    secrets = [
        ACCESS_TOKEN,
        *assertions,
        fixture("g7-token-response")["request"]["body"]["client_assertion"],
        settings.certificate_pem.get_secret_value(),
        settings.private_key_pem.get_secret_value(),
        credential.key_pem.splitlines()[1],
        credential.certificate_pem.splitlines()[1],
    ]
    for text in texts:
        for secret in secrets:
            assert secret not in text
        for private in (MAILBOX, CALENDAR_NAME, CALENDAR_ID, "Mustermann"):
            assert private not in text
    assert output.count("integration_request") == len(fake_graph.requests)
    for template in (TOKEN_PATH_TEMPLATE, CALENDARS_TEMPLATE, CALENDAR_VIEW_TEMPLATE):
        assert f'"{template}"' in output
    assert TENANT_ID not in output
