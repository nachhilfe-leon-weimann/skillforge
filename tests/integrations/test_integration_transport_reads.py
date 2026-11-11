"""The transport's read variants of SF4-1: a form POST that is a read, a path named by its template, Retry-After."""

import asyncio
import json

import httpx
import pytest

from app.core.logging import LogFormat, LoggingSettings, LogLevel, configure_logging
from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.errors import (
    IntegrationError,
    IntegrationRejectedError,
    IntegrationUnavailableError,
    OutcomeUnknownError,
)
from app.integrations.guards import CONTRACT_RUN_ENV
from app.integrations.transport import Exchange, IntegrationHttp
from tests.integrations.chains import carried_texts, carries_a_request, chained

SECRET = "synthetic-form-secret-for-tests"
OBJECT = "objectwithoutdigits"
"""An object id a route word could pass for: ``path_template`` would keep it."""


def _http(handler, exchanges: list[Exchange]) -> IntegrationHttp:
    return IntegrationHttp(
        name="example",
        base_url="https://api.example.org/v1",
        allowed_hosts=frozenset({"api.example.org"}),
        mode=IntegrationMode.DRY_RUN,
        account=IntegrationAccount.TRIAL,
        observer=exchanges.append,
        transport=httpx.MockTransport(handler),
    )


async def test_a_form_post_is_a_read_that_goes_out_in_dry_run_and_is_never_observed():
    requests: list[httpx.Request] = []
    exchanges: list[Exchange] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"token": SECRET})

    async with _http(handler, exchanges) as http:
        reply = await http.read_form("/token", form={"assertion": SECRET})

    assert reply.json() == {"token": SECRET}
    assert [(request.method, request.headers["content-type"]) for request in requests] == [
        ("POST", "application/x-www-form-urlencoded")
    ]
    assert requests[0].content == f"assertion={SECRET}".encode()
    assert exchanges == []


@pytest.fixture
def restore_logging():
    yield
    configure_logging(LoggingSettings())


async def test_a_template_names_the_path_in_the_log_line_and_the_error(restore_logging, capsys):
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()

    async with _http(lambda request: httpx.Response(404), []) as http:
        with pytest.raises(IntegrationRejectedError) as raised:
            await http.read(f"/things/{OBJECT}", template="/things/{thing}")

    output = capsys.readouterr().out
    assert '"/things/{thing}"' in output and "example GET /things/{thing}" in str(raised.value)
    assert OBJECT not in output and OBJECT not in str(raised.value)


@pytest.mark.parametrize(("status", "error"), [(429, IntegrationRejectedError), (503, IntegrationUnavailableError)])
async def test_an_error_carries_the_retry_after_header(status: int, error: type[IntegrationError]):
    async with _http(lambda request: httpx.Response(status, headers={"Retry-After": "7"}), []) as http:
        with pytest.raises(error) as raised:
            await http.read("/things")

    assert raised.value.retry_after == "7"


# --- a failure in transit leaves no httpx exception behind ---

RAW_TOKEN = "synthetic-raw-token-for-the-chain-test"


@pytest.mark.parametrize(
    ("send", "error"),
    [
        (lambda http: http.read("/things"), IntegrationUnavailableError),
        (lambda http: http.write("POST", "/things", json={}), OutcomeUnknownError),
    ],
    ids=["a read", "a write"],
)
async def test_a_failure_in_transit_carries_no_token_and_no_request(send, error: type[IntegrationError], monkeypatch):
    monkeypatch.setenv(CONTRACT_RUN_ENV, "1")  # live mode, so the write is sent

    def handler(request: httpx.Request) -> httpx.Response:
        # h11 names the offending header value - here the raw token - in its message.
        raise httpx.LocalProtocolError(f"Illegal header value b'{request.headers['Authorization']} '", request=request)

    http = IntegrationHttp(
        name="example",
        base_url="https://api.example.org/v1",
        allowed_hosts=frozenset({"api.example.org"}),
        credentials={"Authorization": RAW_TOKEN},
        mode=IntegrationMode.LIVE,
        account=IntegrationAccount.TRIAL,
        transport=httpx.MockTransport(handler),
    )
    async with http:
        with pytest.raises(error) as raised:
            await send(http)

    assert not carries_a_request(raised.value), [type(current).__name__ for current in chained(raised.value)]
    assert all(RAW_TOKEN not in text for text in carried_texts(raised.value))


# --- a cancelled request writes its line and stays cancelled ---


@pytest.mark.parametrize(
    ("send", "method"),
    [(lambda http: http.read("/things/4711"), "GET"), (lambda http: http.write("POST", "/things", json={}), "POST")],
    ids=["a read", "a write"],
)
async def test_a_cancelled_request_writes_its_line_and_is_cancelled_still(
    send, method: str, restore_logging, capsys, monkeypatch
):
    monkeypatch.setenv(CONTRACT_RUN_ENV, "1")  # live mode, so the write is sent
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()
    started = asyncio.Event()

    async def hanging(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(10)
        return httpx.Response(200, json={})

    http = IntegrationHttp(
        name="example",
        base_url="https://api.example.org/v1",
        allowed_hosts=frozenset({"api.example.org"}),
        credentials={"Authorization": RAW_TOKEN},
        mode=IntegrationMode.LIVE,
        account=IntegrationAccount.TRIAL,
        transport=httpx.MockTransport(hanging),
    )
    async with http:
        task = asyncio.create_task(send(http))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    output = capsys.readouterr().out
    lines = [json.loads(line) for line in output.splitlines() if '"integration_request"' in line]
    assert [(line["method"], line["outcome"], line["status"]) for line in lines] == [(method, "cancelled", None)]
    assert lines[0]["path"].startswith("/v1/things") and isinstance(lines[0]["duration_ms"], int)
    assert "4711" not in output and RAW_TOKEN not in output
