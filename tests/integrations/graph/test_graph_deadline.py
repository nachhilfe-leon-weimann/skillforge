"""One read ends within ``READ_DEADLINE_SECONDS``: no send and no wait without time left, a send cut at the deadline,
and then nothing more - on an injected clock and sleep, so no test waits."""

import asyncio
import json

import httpx
import pytest

from app.core.logging import LogFormat, LoggingSettings, LogLevel, configure_logging
from app.integrations.errors import IdentityCheckError, IntegrationUnavailableError
from app.integrations.graph.client import (
    CALENDAR_VIEW_TEMPLATE,
    MAX_RETRY_AFTER_SECONDS,
    READ_DEADLINE_SECONDS,
    GraphClient,
)
from app.integrations.graph.config import GraphSettings
from app.integrations.graph.credential import TokenCache
from tests.integrations.chains import carried_texts
from tests.integrations.graph.fake_graph import (
    ACCESS_TOKEN,
    CALENDAR_ID,
    CALENDAR_NAME,
    CALENDARS,
    MAILBOX,
    TOKEN_REQUEST,
    VIEW_PATH,
    WINDOW,
    FakeGraph,
    answer,
    with_next_link,
)

NEXT_PAGE = f"https://graph.microsoft.com{VIEW_PATH}?$skip=1"
"""A next page that always follows: a listing that ends only at the deadline (or the page cap)."""


class Clock:
    """A monotonic clock the test moves: by every sleep, and by a slow answer."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


async def read(
    settings: GraphSettings, fake: FakeGraph, clock: Clock, transport: httpx.MockTransport | None = None
) -> None:
    async def sleep(seconds: float) -> None:
        fake.sleeps.append(seconds)
        clock.now += seconds

    async with GraphClient.connect(
        settings, transport=transport or fake.transport, tokens=TokenCache(), sleep=sleep, clock=clock
    ) as client:
        await client.calendar_view(*WINDOW)


def views(fake: FakeGraph) -> int:
    return sum(request.url.path == VIEW_PATH for request in fake.requests)


def throttled(seconds: int) -> httpx.Response:
    return httpx.Response(429, headers={"Retry-After": str(seconds)})


def assert_names_nothing(error: BaseException) -> None:
    for text in carried_texts(error):
        for private in (MAILBOX, CALENDAR_ID, CALENDAR_NAME, ACCESS_TOKEN, "Mustermann"):
            assert private not in text


async def test_a_read_throttled_page_after_page_stops_at_the_deadline(settings, fake_graph: FakeGraph, clock: Clock):
    page = with_next_link("g1-calendar-view-page-1", NEXT_PAGE)
    every_page = [throttled(MAX_RETRY_AFTER_SECONDS), throttled(MAX_RETRY_AFTER_SECONDS), page]
    fake_graph.on("GET", VIEW_PATH, *(every_page * 10))

    with pytest.raises(IntegrationUnavailableError, match="deadline") as raised:
        await read(settings, fake_graph, clock)

    # Page 1: two waits, then the page (60 s). Page 2: one wait (90 s); the next wait would end at the deadline.
    assert fake_graph.sleeps == [MAX_RETRY_AFTER_SECONDS] * 3
    assert views(fake_graph) == 5
    assert_names_nothing(raised.value)


async def test_no_send_starts_once_the_deadline_is_reached(settings, fake_graph: FakeGraph, clock: Clock):
    def slow_page(request: httpx.Request) -> httpx.Response:
        clock.now += 50
        return with_next_link("g1-calendar-view-page-1", NEXT_PAGE)

    fake_graph.on("GET", VIEW_PATH, slow_page)

    with pytest.raises(IntegrationUnavailableError, match="deadline") as raised:
        await read(settings, fake_graph, clock)

    assert views(fake_graph) == 3, "pages sent at 0, 50 and 100 s; none at 150 s"
    assert fake_graph.sleeps == []
    assert_names_nothing(raised.value)


async def test_a_wait_that_would_cross_the_deadline_is_not_taken(settings, fake_graph: FakeGraph, clock: Clock):
    def late_and_throttled(request: httpx.Request) -> httpx.Response:
        clock.now += READ_DEADLINE_SECONDS - 10
        return throttled(MAX_RETRY_AFTER_SECONDS)

    fake_graph.on("GET", VIEW_PATH, late_and_throttled)

    with pytest.raises(IntegrationUnavailableError, match="deadline"):
        await read(settings, fake_graph, clock)

    assert fake_graph.sleeps == [] and views(fake_graph) == 1


@pytest.fixture
def restore_logging():
    yield
    configure_logging(LoggingSettings())


def cancelled_lines(output: str) -> list[dict]:
    lines = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("event") == "integration_request" and line["outcome"] == "cancelled"]


async def test_a_send_still_running_at_the_deadline_is_cut(
    settings, fake_graph: FakeGraph, clock: Clock, restore_logging, capsys
):
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()

    def nearly_late(request: httpx.Request) -> httpx.Response:
        clock.now += READ_DEADLINE_SECONDS - 0.05  # 50 ms of real time left for the next send
        return answer("g1-calendars")

    async def hanging_view(request: httpx.Request) -> httpx.Response:
        if request.url.path == VIEW_PATH:
            fake_graph.requests.append(request)
            await asyncio.sleep(10)
        return fake_graph(request)

    fake_graph.on("GET", CALENDARS["request"]["path"], nearly_late)

    with pytest.raises(IntegrationUnavailableError, match="deadline") as raised:
        await asyncio.wait_for(read(settings, fake_graph, clock, httpx.MockTransport(hanging_view)), timeout=5)

    assert views(fake_graph) == 1
    assert_names_nothing(raised.value)
    output = capsys.readouterr().out
    (line,) = cancelled_lines(output)
    assert (line["method"], line["path"], line["status"]) == ("GET", CALENDAR_VIEW_TEMPLATE, None)
    assert isinstance(line["duration_ms"], int)
    assert MAILBOX not in output and CALENDAR_ID not in output and ACCESS_TOKEN not in output


async def test_a_hanging_token_request_is_cut_at_the_deadline(settings, fake_graph: FakeGraph):
    times = iter([1000.0])  # the deadline starts at 1000 s; every later reading is 50 ms before it

    def late_clock() -> float:
        return next(times, 1000.0 + READ_DEADLINE_SECONDS - 0.05)

    async def hanging_token(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            fake_graph.requests.append(request)
            await asyncio.sleep(10)
        return fake_graph(request)

    async def read_once() -> None:
        async with GraphClient.connect(
            settings,
            transport=httpx.MockTransport(hanging_token),
            tokens=TokenCache(),
            sleep=fake_graph.sleep,
            clock=late_clock,
        ) as client:
            await client.calendar_view(*WINDOW)

    with pytest.raises(IntegrationUnavailableError, match=r"POST /\{tenant\}/oauth2/v2.0/token: .*deadline"):
        await asyncio.wait_for(read_once(), timeout=5)

    assert [request.method for request in fake_graph.requests] == ["POST"], "no identity read after the cut"


async def test_the_token_request_counts_against_the_deadline(settings, fake_graph: FakeGraph, clock: Clock):
    def slow_token(request: httpx.Request) -> httpx.Response:
        clock.now += READ_DEADLINE_SECONDS
        return answer("g7-token-response")

    fake_graph.on("POST", TOKEN_REQUEST["path"], slow_token)

    # The identity read is where the deadline is found: a failed identity read is an IdentityCheckError, as always.
    with pytest.raises(IdentityCheckError, match="IntegrationUnavailableError"):
        await read(settings, fake_graph, clock)

    assert [request.method for request in fake_graph.requests] == ["POST"], "no identity read after the deadline"


async def test_a_read_inside_the_deadline_is_unaffected(settings, fake_graph: FakeGraph, clock: Clock):
    fake_graph.on(
        "GET",
        VIEW_PATH,
        throttled(MAX_RETRY_AFTER_SECONDS),
        throttled(MAX_RETRY_AFTER_SECONDS),
        with_next_link("g1-calendar-view-page-1", None),
    )

    await read(settings, fake_graph, clock)

    assert fake_graph.sleeps == [MAX_RETRY_AFTER_SECONDS] * 2 and views(fake_graph) == 3
