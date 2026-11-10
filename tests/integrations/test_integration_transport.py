"""The guarded transport: allowlist, no network under pytest, dry_run, no retry, outcome unknown, no secret leaks."""

import json
import logging
import subprocess
import sys
import traceback
from decimal import Decimal

import httpx
import pytest

from app.core.logging import LogFormat, LoggingSettings, LogLevel, configure_logging
from app.integrations import transport
from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.errors import (
    IntegrationError,
    IntegrationRefusedError,
    IntegrationRejectedError,
    IntegrationUnavailableError,
    OutcomeUnknownError,
)
from app.integrations.guards import CONTRACT_RUN_ENV, ensure_live_allowed, ensure_network_allowed
from app.integrations.transport import Exchange, IntegrationHttp, PlannedRequest, Reply, path_template

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


def _http(
    handler: Recorded | None = None, *, mode=IntegrationMode.LIVE, account=IntegrationAccount.TRIAL, **kwargs
) -> IntegrationHttp:
    return IntegrationHttp(
        name="example",
        base_url=BASE_URL,
        allowed_hosts=frozenset({"api.example.org"}),
        headers={"Accept": "application/json"},
        credentials={"Authorization": TOKEN},
        mode=mode,
        account=account,
        transport=httpx.MockTransport(handler) if handler else None,
        **kwargs,
    )


@pytest.fixture
def contract_run(monkeypatch):
    """Live mode needs the contract run under pytest."""
    monkeypatch.setenv(CONTRACT_RUN_ENV, "1")


@pytest.fixture
def network(monkeypatch) -> list[httpx.Request]:
    """Stands in for the socket, so a broken guard fails a test instead of reaching the network."""
    reached: list[httpx.Request] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        reached.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    return reached


# --- guards ---

FOREIGN_URLS = [
    "https://elsewhere.example.org/v1/x",
    "https://api.example.org.evil.test/v1/x",
    "https://evilapi.example.org/v1/x",
    "http://api.example.org/v1/x",
    "https://api.example.org:8443/v1/x",
    "https://user:secret@api.example.org/v1/x",
]


@pytest.mark.usefixtures("contract_run")
@pytest.mark.parametrize("url", FOREIGN_URLS)
async def test_a_url_outside_the_allowlist_is_refused_before_sending(url: str):
    handler = Recorded()

    async with _http(handler) as http:
        with pytest.raises(IntegrationRefusedError, match="allowlist"):
            await http.read(url)
        with pytest.raises(IntegrationRefusedError, match="allowlist"):
            await http.write("POST", url, json={})

    assert handler.requests == []


@pytest.mark.usefixtures("contract_run")
@pytest.mark.parametrize(
    "path", ["/../admin", "/x/../../admin", "https://api.example.org/admin", "https://api.example.org/v10/x"]
)
async def test_a_path_outside_the_base_url_is_refused(path: str):
    handler = Recorded()

    async with _http(handler) as http:
        with pytest.raises(IntegrationRefusedError, match="outside the base URL"):
            await http.read(path)

    assert handler.requests == []


async def test_a_dry_run_write_to_a_foreign_host_is_refused_too():
    async with _http(Recorded(), mode=IntegrationMode.DRY_RUN) as http:
        with pytest.raises(IntegrationRefusedError, match="allowlist"):
            await http.write("POST", "https://elsewhere.example.org/v1/x", json={})


async def test_real_network_is_refused_under_pytest(network):
    async with _http(None, mode=IntegrationMode.DRY_RUN) as http:
        with pytest.raises(IntegrationRefusedError, match="real network"):
            await http.read("/anything")

    assert network == []


@pytest.mark.usefixtures("contract_run")
async def test_the_contract_run_opens_the_network_for_a_trial_account_only(network):
    """A dry_run read in the contract run must not reach a live account."""
    async with _http(None, mode=IntegrationMode.DRY_RUN, account=IntegrationAccount.LIVE) as http:
        with pytest.raises(IntegrationRefusedError, match="trial account only"):
            await http.read("/anything")
    assert network == []

    async with _http(None, mode=IntegrationMode.DRY_RUN) as http:
        await http.read("/anything")
    assert [request.url.path for request in network] == ["/v1/anything"]


def test_the_network_guard_opens_only_for_the_contract_run_on_a_trial(monkeypatch):
    with pytest.raises(IntegrationRefusedError):
        ensure_network_allowed(IntegrationAccount.TRIAL)

    monkeypatch.setenv(CONTRACT_RUN_ENV, "1")
    ensure_network_allowed(IntegrationAccount.TRIAL)
    with pytest.raises(IntegrationRefusedError):
        ensure_network_allowed(IntegrationAccount.LIVE)

    monkeypatch.setenv(CONTRACT_RUN_ENV, "true")
    with pytest.raises(IntegrationRefusedError):
        ensure_network_allowed(IntegrationAccount.TRIAL)


def test_a_subprocess_of_a_test_counts_as_under_pytest():
    code = "from app.integrations.guards import running_under_pytest; print(running_under_pytest())"

    assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout == "True\n"


def test_a_test_run_with_the_contract_flag_but_without_the_contract_marker_fails_loudly(monkeypatch):
    monkeypatch.setenv(CONTRACT_RUN_ENV, "1")
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/integrations/test_integrations_architecture.py",
        "-p",
        "no:cacheprovider",
    ]

    result = subprocess.run(command, capture_output=True, text=True)

    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "it is for `pytest -m contract` only" in result.stderr


def test_live_mode_is_refused_under_pytest_without_the_contract_run():
    with pytest.raises(IntegrationRefusedError, match="live mode"):
        _http(Recorded())


@pytest.mark.usefixtures("contract_run")
def test_live_mode_is_refused_under_pytest_for_a_live_account():
    with pytest.raises(IntegrationRefusedError, match="trial account only"):
        ensure_live_allowed(IntegrationAccount.LIVE)
    with pytest.raises(IntegrationRefusedError, match="trial account only"):
        _http(Recorded(), account=IntegrationAccount.LIVE)


@pytest.mark.usefixtures("contract_run")
def test_live_mode_is_allowed_under_pytest_only_in_the_contract_run_for_a_trial_account():
    ensure_live_allowed(IntegrationAccount.TRIAL)
    _http(Recorded())


def test_dry_run_needs_no_contract_run():
    _http(Recorded(), mode=IntegrationMode.DRY_RUN)


async def test_the_network_transport_never_retries_and_no_redirect_is_followed(monkeypatch):
    built: list[dict] = []

    class Spy(httpx.AsyncHTTPTransport):
        def __init__(self, **kwargs) -> None:
            built.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", Spy)
    http = _http(None, mode=IntegrationMode.DRY_RUN)

    assert built == [{"retries": 0}]
    async with http:
        assert http._client.follow_redirects is False


async def test_the_client_is_used_inside_its_block_only():
    http = _http(Recorded(), mode=IntegrationMode.DRY_RUN)

    with pytest.raises(RuntimeError, match="outside its `async with` block"):
        await http.read("/Things")


# --- modes ---


async def test_a_dry_run_write_returns_the_planned_request_and_sends_nothing():
    handler = Recorded()

    async with _http(handler, mode=IntegrationMode.DRY_RUN) as http:
        planned = await http.write("POST", "/Things", params={"tag": "a"}, json={"name": "Example"})
        read = await http.read("/Things")

    assert planned == PlannedRequest("POST", f"{BASE_URL}/Things?tag=a", {"name": "Example"})
    assert [request.method for request in handler.requests] == ["GET"]
    assert isinstance(read, Reply) and read.json() == {"ok": True}


@pytest.mark.usefixtures("contract_run")
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


@pytest.mark.usefixtures("contract_run")
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
        with pytest.raises(OutcomeUnknownError, match="outcome is unknown") as raised:
            await http.write(method, "/Things", json={})

    assert len(handler.requests) == 1
    assert type(failure).__name__ in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


@pytest.mark.usefixtures("contract_run")
async def test_a_read_that_fails_in_transit_is_unavailable_and_not_retried():
    handler = Recorded(raises=httpx.ConnectError("refused"))

    async with _http(handler) as http:
        with pytest.raises(IntegrationUnavailableError, match="ConnectError"):
            await http.read("/Things")

    assert len(handler.requests) == 1


@pytest.mark.usefixtures("contract_run")
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirected_write_is_neither_followed_nor_a_success(status: int):
    handler = Recorded(httpx.Response(status, headers={"Location": f"{BASE_URL}/Elsewhere"}))

    async with _http(handler) as http:
        with pytest.raises(OutcomeUnknownError):
            await http.write("POST", "/Things", json={})

    assert [request.url.path for request in handler.requests] == ["/v1/Things"]


def _message(reply: Reply) -> str | None:
    match reply.json():
        case {"error": {"message": str(message)}}:
            return message
    return None


@pytest.mark.usefixtures("contract_run")
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


@pytest.mark.usefixtures("contract_run")
async def test_a_failing_observer_never_hides_the_outcome_of_a_write():
    handler = Recorded(httpx.Response(201, json={"id": "7"}))

    def observer(exchange: Exchange) -> None:
        raise OSError("disk full")

    async with _http(handler, observer=observer) as http:
        reply = await http.write("POST", "/Things", json={})

    assert isinstance(reply, Reply) and reply.status == 201


@pytest.mark.usefixtures("contract_run")
@pytest.mark.parametrize("write", [False, True])
async def test_an_unreadable_error_body_keeps_the_error(write: bool):
    handler = Recorded(httpx.Response(500, content=b"<html>not json</html>"))

    async with _http(handler, error_message=_message) as http:
        with pytest.raises(OutcomeUnknownError if write else IntegrationUnavailableError) as raised:
            await (http.write("POST", "/Things", json={}) if write else http.read("/Things"))

    assert raised.value.status == 500 and raised.value.remote_message is None


class _FailingLogger:
    """Fails on the line logged once the response is in: a failure after the request went out."""

    def __init__(self, failure: BaseException) -> None:
        self._failure = failure

    def info(self, event: str, **values: object) -> None:
        if values.get("outcome") == "response":
            raise self._failure

    def warning(self, event: str, **values: object) -> None:
        pass


@pytest.mark.usefixtures("contract_run")
@pytest.mark.parametrize("failure", [RecursionError("deep"), ValueError("bad"), KeyError("x")])
async def test_any_failure_after_a_write_was_sent_leaves_its_outcome_unknown(monkeypatch, failure: Exception):
    monkeypatch.setattr(transport, "logger", _FailingLogger(failure))
    handler = Recorded(httpx.Response(201, json={"id": "7"}))

    async with _http(handler) as http:
        with pytest.raises(OutcomeUnknownError, match=f"answer failed \\({type(failure).__name__}\\)") as raised:
            await http.write("POST", "/Things", json={})
        with pytest.raises(type(failure)):
            await http.read("/Things")

    assert raised.value.status == 201 and raised.value.__cause__ is None
    assert len(handler.requests) == 2


@pytest.mark.usefixtures("contract_run")
async def test_an_unexpected_failure_while_a_write_is_sent_leaves_its_outcome_unknown():
    handler = Recorded(raises=RuntimeError("synthetic failure inside the transport"))

    async with _http(handler) as http:
        with pytest.raises(OutcomeUnknownError, match=r"failed \(RuntimeError\); its outcome is unknown"):
            await http.write("PUT", "/Things", json={})
        with pytest.raises(RuntimeError):
            await http.read("/Things")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"price": 0.1}', {"price": Decimal("0.1")}),
        (b'{"price": 90071992547409.93}', {"price": Decimal("90071992547409.93")}),
        (b'{"sum": 9007199254740993, "share": 1e-2}', {"sum": 9007199254740993, "share": Decimal("0.01")}),
    ],
)
def test_a_json_number_never_passes_through_float(body: bytes, expected: dict):
    parsed = Reply(200, body).json()

    assert parsed == expected
    assert isinstance(parsed, dict) and not any(isinstance(value, float) for value in parsed.values())


# --- no id in a log line or an error message ---


@pytest.mark.parametrize(
    ("path", "template"),
    [
        ("/v1/Things", "/v1/Things"),
        ("/api/v1/Contact/4711", "/api/v1/Contact/{id}"),
        ("/api/v1/Invoice/4711/getPositions", "/api/v1/Invoice/{id}/getPositions"),
        ("/v1/Contact/family-a@example.org", "/v1/Contact/{id}"),
        ("/v1/Contact/a1b2", "/v1/Contact/{id}"),
        ("/v1/Contact/x.y", "/v1/Contact/{id}"),
        ("/v1/Contact/", "/v1/Contact/"),
    ],
)
def test_a_path_template_names_no_object(path: str, template: str):
    assert path_template(path) == template


@pytest.mark.usefixtures("contract_run")
async def test_no_id_appears_in_a_log_line_or_an_error_message(restore_logging, capsys, caplog):
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()
    contact = "4711"
    messages: list[str] = []
    responses = iter([httpx.Response(200, json={}), httpx.Response(404, json={}), httpx.Response(500, json={})])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            raise httpx.ReadTimeout("timed out", request=request)
        return next(responses, httpx.Response(503, json={}))

    calls = [
        lambda http: http.read(f"/Contact/{contact}"),
        lambda http: http.read(f"/Contact/{contact}/addresses"),
        lambda http: http.read(f"/Invoice/{contact}/getPositions"),
        lambda http: http.write("PUT", f"/Contact/{contact}", json={}),
        lambda http: http.write("DELETE", f"/Contact/{contact}"),
        lambda http: http.read(f"https://api.example.org/elsewhere/{contact}"),
    ]
    async with _http(Recorded(), error_message=_message) as http:
        http._guard._inner = httpx.MockTransport(handler)
        for call in calls:
            try:
                await call(http)
            except IntegrationError as exc:
                messages.extend((str(exc), repr(exc), "".join(traceback.format_exception(exc))))

    output = capsys.readouterr().out
    assert len(messages) == 3 * 5
    assert output.count("integration_request") == 5
    assert "/v1/Contact/{id}" in output and "Contact/{id}" in messages[0]
    for text in (output, caplog.text, *messages):
        assert contact not in text
    assert all(logging.getLogger(library).getEffectiveLevel() >= logging.WARNING for library in ("httpx", "httpcore"))


# --- the credential stays inside ---


@pytest.fixture
def restore_logging():
    yield
    configure_logging(LoggingSettings())


@pytest.mark.usefixtures("contract_run")
async def test_the_credential_appears_in_no_repr_log_error_traceback_or_exchange(restore_logging, capsys, caplog):
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
        if request.url.path.endswith("/protocol"):
            # h11 names the offending header value - the raw token - in its message.
            raise httpx.LocalProtocolError(f"Illegal header value b'{TOKEN} '", request=request)
        return next(responses)

    http = IntegrationHttp(
        name="example",
        base_url=BASE_URL,
        allowed_hosts=frozenset({"api.example.org"}),
        headers={"Accept": "application/json"},
        credentials={"Authorization": TOKEN},
        mode=IntegrationMode.LIVE,
        account=IntegrationAccount.TRIAL,
        observer=exchanges.append,
        transport=httpx.MockTransport(handler),
    )
    async with http:
        await http.read("/Things")
        for call in (
            http.write("POST", "/Things", json={}),
            http.write("PUT", "/timeout", json={}),
            http.write("PUT", "/protocol", json={}),
        ):
            try:
                await call
            except OutcomeUnknownError as exc:
                errors.append(exc)

    output = capsys.readouterr().out
    assert output.count("integration_request") == 4
    assert len(errors) == 3
    tracebacks = ["".join(traceback.format_exception(error)) for error in errors]
    for text in (repr(http), output, caplog.text, *tracebacks, *map(repr, errors), *map(repr, exchanges)):
        assert TOKEN not in text
    assert exchanges[0].request_headers["authorization"] == "[REDACTED]"
    assert exchanges[0].request_headers["accept"] == "application/json"
    assert exchanges[0].response_headers["set-cookie"] == "[REDACTED]"
