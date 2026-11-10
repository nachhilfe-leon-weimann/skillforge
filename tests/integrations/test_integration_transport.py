"""The guarded transport: allowlist, no network under pytest, dry_run, no retry, outcome unknown, no secret leaks."""

import json

import httpx
import pytest

from app.core.logging import LogFormat, LoggingSettings, LogLevel, configure_logging
from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.errors import (
    IntegrationError,
    IntegrationRefusedError,
    IntegrationRejectedError,
    IntegrationUnavailableError,
    OutcomeUnknownError,
)
from app.integrations.guards import CONTRACT_RUN_ENV, ensure_live_allowed, ensure_network_allowed
from app.integrations.transport import Exchange, IntegrationHttp, PlannedRequest, Reply

TOKEN = "synthetic-credential-for-tests-only"
BASE_URL = "https://api.example.org/v1"


class Recorded:
    """A MockTransport handler that answers with a fixed response, or raises, and remembers every request."""

    def __init__(self, response: httpx.Response | None = None, *, raises: Exception | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response or httpx.Response(200, json={"ok": True})
        self._raises = raises

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._raises is not None:
            raise self._raises
        return self._response


def _http(handler: Recorded | None = None, *, mode=IntegrationMode.LIVE, **kwargs) -> IntegrationHttp:
    return IntegrationHttp(
        name="example",
        base_url=BASE_URL,
        allowed_hosts=frozenset({"api.example.org"}),
        headers={"Authorization": TOKEN},
        mode=mode,
        account=IntegrationAccount.TRIAL,
        transport=httpx.MockTransport(handler) if handler else None,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def contract_run(monkeypatch):
    """Live mode needs the contract run under pytest; tests that check the refusal remove it again."""
    monkeypatch.setenv(CONTRACT_RUN_ENV, "1")


# --- guards ---


@pytest.mark.parametrize("url", ["https://elsewhere.example.org/v1/x", "http://api.example.org/v1/x"])
async def test_a_host_outside_the_allowlist_is_refused_before_sending(url: str):
    handler = Recorded()

    async with _http(handler) as http:
        with pytest.raises(IntegrationRefusedError, match="allowlist"):
            await http.read(url)
        with pytest.raises(IntegrationRefusedError, match="allowlist"):
            await http.write("POST", url, json={})

    assert handler.requests == []


async def test_a_dry_run_write_to_a_foreign_host_is_refused_too():
    async with _http(Recorded(), mode=IntegrationMode.DRY_RUN) as http:
        with pytest.raises(IntegrationRefusedError, match="allowlist"):
            await http.write("POST", "https://elsewhere.example.org/x", json={})


async def test_real_network_is_refused_under_pytest(monkeypatch):
    monkeypatch.delenv(CONTRACT_RUN_ENV)
    # Stands in for the socket, so a broken guard fails this test instead of reaching the network.
    reached: list[httpx.Request] = []

    async def network(self, request: httpx.Request) -> httpx.Response:
        reached.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)

    async with _http(None, mode=IntegrationMode.DRY_RUN) as http:
        with pytest.raises(IntegrationRefusedError, match="real network"):
            await http.read("/anything")

    assert reached == []


def test_the_network_guard_opens_only_for_the_contract_run(monkeypatch):
    ensure_network_allowed()  # the fixture set the contract run

    monkeypatch.setenv(CONTRACT_RUN_ENV, "true")
    with pytest.raises(IntegrationRefusedError):
        ensure_network_allowed()


def test_live_mode_is_refused_under_pytest_without_the_contract_run(monkeypatch):
    monkeypatch.delenv(CONTRACT_RUN_ENV)

    with pytest.raises(IntegrationRefusedError, match="live mode"):
        _http(Recorded())


def test_live_mode_is_refused_under_pytest_for_a_live_account():
    with pytest.raises(IntegrationRefusedError, match="trial account only"):
        ensure_live_allowed(IntegrationAccount.LIVE)
    with pytest.raises(IntegrationRefusedError, match="trial account only"):
        IntegrationHttp(
            name="example",
            base_url=BASE_URL,
            allowed_hosts=frozenset({"api.example.org"}),
            headers={},
            mode=IntegrationMode.LIVE,
            account=IntegrationAccount.LIVE,
            transport=httpx.MockTransport(Recorded()),
        )


def test_live_mode_is_allowed_under_pytest_only_in_the_contract_run_for_a_trial_account():
    ensure_live_allowed(IntegrationAccount.TRIAL)
    _http(Recorded())


def test_dry_run_needs_no_contract_run(monkeypatch):
    monkeypatch.delenv(CONTRACT_RUN_ENV)

    _http(Recorded(), mode=IntegrationMode.DRY_RUN)


# --- modes ---


async def test_a_dry_run_write_returns_the_planned_request_and_sends_nothing():
    handler = Recorded()

    async with _http(handler, mode=IntegrationMode.DRY_RUN) as http:
        planned = await http.write("POST", "/Things", params={"tag": "a"}, json={"name": "Example"})
        read = await http.read("/Things")

    assert planned == PlannedRequest("POST", f"{BASE_URL}/Things?tag=a", {"name": "Example"})
    assert [request.method for request in handler.requests] == ["GET"]
    assert isinstance(read, Reply) and read.json() == {"ok": True}


async def test_a_live_write_is_sent():
    handler = Recorded(httpx.Response(201, json={"id": "7"}))

    async with _http(handler) as http:
        reply = await http.write("POST", "/Things", json={"name": "Example"})

    assert isinstance(reply, Reply) and reply.status == 201
    assert [(r.method, r.url.path, json.loads(r.content)) for r in handler.requests] == [
        ("POST", "/v1/Things", {"name": "Example"})
    ]
    assert handler.requests[0].headers["authorization"] == TOKEN


# --- failures: never retried, a write's outcome is unknown ---


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("timed out"),
        httpx.WriteTimeout("timed out"),
        httpx.ConnectTimeout("timed out"),
        httpx.ConnectError("refused"),
        httpx.RemoteProtocolError("closed"),
    ],
)
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
async def test_a_write_that_fails_in_transit_is_not_retried_and_its_outcome_is_unknown(failure, method):
    handler = Recorded(raises=failure)

    async with _http(handler) as http:
        with pytest.raises(OutcomeUnknownError, match="outcome is unknown"):
            await http.write(method, "/Things", json={})

    assert len(handler.requests) == 1


async def test_a_read_that_fails_in_transit_is_unavailable_and_not_retried():
    handler = Recorded(raises=httpx.ConnectError("refused"))

    async with _http(handler) as http:
        with pytest.raises(IntegrationUnavailableError):
            await http.read("/Things")

    assert len(handler.requests) == 1


def _message(reply: Reply) -> str | None:
    match reply.json():
        case {"error": {"message": str(message)}}:
            return message
    return None


@pytest.mark.parametrize(
    ("write", "status", "error"),
    [
        (True, 400, IntegrationRejectedError),
        (True, 422, IntegrationRejectedError),
        (True, 500, OutcomeUnknownError),
        (True, 503, OutcomeUnknownError),
        (False, 404, IntegrationRejectedError),
        (False, 500, IntegrationUnavailableError),
    ],
)
async def test_an_error_status_maps_to_the_error_model(write: bool, status: int, error: type[IntegrationError]):
    handler = Recorded(httpx.Response(status, json={"error": {"message": "Synthetic remote message"}}))

    async with _http(handler, error_message=_message) as http:
        with pytest.raises(error) as raised:
            await (http.write("POST", "/Things", json={}) if write else http.read("/Things"))

    assert raised.value.status == status
    assert raised.value.remote_message == "Synthetic remote message"
    assert "Synthetic remote message" not in str(raised.value)
    assert len(handler.requests) == 1


# --- the credential stays inside ---


@pytest.fixture
def restore_logging():
    yield
    configure_logging(LoggingSettings())


async def test_the_credential_appears_in_no_repr_log_error_or_exchange(restore_logging, capsys, caplog):
    # Configured inside the test: the handler writes to the stream that is current when it is set up.
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()
    exchanges: list[Exchange] = []
    errors: list[Exception] = []
    responses = iter([
        httpx.Response(200, json={}, headers={"Set-Cookie": "session=synthetic"}),
        httpx.Response(500, json={"error": {"message": "x"}}),
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/timeout"):
            raise httpx.ReadTimeout("timed out", request=request)
        return next(responses)

    http = IntegrationHttp(
        name="example",
        base_url=BASE_URL,
        allowed_hosts=frozenset({"api.example.org"}),
        headers={"Authorization": TOKEN},
        mode=IntegrationMode.LIVE,
        account=IntegrationAccount.TRIAL,
        observer=exchanges.append,
        transport=httpx.MockTransport(handler),
    )
    async with http:
        await http.read("/Things")
        for call in (http.write("POST", "/Things", json={}), http.write("PUT", "/timeout", json={})):
            try:
                await call
            except OutcomeUnknownError as exc:
                errors.append(exc)

    output = capsys.readouterr().out
    assert output.count("integration_request") == 3
    assert len(errors) == 2
    for text in (repr(http), output, caplog.text, *map(str, errors), *map(repr, errors), *map(repr, exchanges)):
        assert TOKEN not in text
    assert exchanges[0].request_headers["authorization"] == "[REDACTED]"
    assert exchanges[0].response_headers["set-cookie"] == "[REDACTED]"
