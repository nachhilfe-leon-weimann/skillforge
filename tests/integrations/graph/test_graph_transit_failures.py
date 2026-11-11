"""A request that fails in transit leaves no httpx exception behind: neither the bearer token nor the assertion, nor
the request that held them, is reachable from the error - not as cause, not as suppressed context."""

from collections.abc import Callable

import httpx
import pytest

from app.integrations.errors import IdentityCheckError, IntegrationError, IntegrationUnavailableError
from app.integrations.graph.client import GraphClient
from app.integrations.graph.config import GraphSettings
from app.integrations.graph.credential import TokenCache
from tests.integrations.chains import carried_texts, carries_a_request, chained
from tests.integrations.graph.fake_graph import ACCESS_TOKEN, CALENDARS, TOKEN_REQUEST, VIEW_PATH, WINDOW, FakeGraph


def failing(error: Callable[[httpx.Request], httpx.RequestError]) -> Callable[[httpx.Request], httpx.Response]:
    """A route that fails in transit with an error naming what the request carried, as h11 or httpcore may."""

    def answer(request: httpx.Request) -> httpx.Response:
        raise error(request)

    return answer


def naming_the_token(request: httpx.Request) -> httpx.RequestError:
    return httpx.LocalProtocolError(f"Illegal header value b'{request.headers['Authorization']} '", request=request)


def naming_the_form(request: httpx.Request) -> httpx.RequestError:
    return httpx.ReadTimeout(f"timed out sending {request.content.decode()}", request=request)


async def read(settings: GraphSettings, fake: FakeGraph) -> None:
    async with GraphClient.connect(settings, transport=fake.transport, tokens=TokenCache(), sleep=fake.sleep) as client:
        await client.calendar_view(*WINDOW)


CASES = {
    "a calendar read with the bearer token": (
        ("GET", VIEW_PATH, naming_the_token),
        IntegrationUnavailableError,
    ),
    "the identity read with the bearer token": (
        ("GET", CALENDARS["request"]["path"], naming_the_token),
        IdentityCheckError,
    ),
    "the token request with its assertion": (
        ("POST", TOKEN_REQUEST["path"], naming_the_form),
        IntegrationUnavailableError,
    ),
}


@pytest.mark.parametrize(("route", "expected"), CASES.values(), ids=CASES.keys())
async def test_a_failure_in_transit_carries_no_secret_and_no_request(
    route, expected: type[IntegrationError], settings, fake_graph: FakeGraph
):
    method, path, error = route
    fake_graph.on(method, path, failing(error))

    with pytest.raises(expected) as raised:
        await read(settings, fake_graph)

    assertions = [
        httpx.QueryParams(request.content.decode())["client_assertion"]
        for request in fake_graph.requests
        if request.method == "POST"
    ]
    assert assertions, "the token request was sent"
    assert not carries_a_request(raised.value), [type(current).__name__ for current in chained(raised.value)]
    for text in carried_texts(raised.value):
        assert ACCESS_TOKEN not in text
        for assertion in assertions:
            assert assertion not in text
