"""The transport's read variants of SF4-1: a form POST that is a read, a path named by its template, Retry-After."""

import httpx
import pytest

from app.core.logging import LogFormat, LoggingSettings, LogLevel, configure_logging
from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.errors import IntegrationError, IntegrationRejectedError, IntegrationUnavailableError
from app.integrations.transport import Exchange, IntegrationHttp

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
