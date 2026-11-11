"""The calendar read: paged to the end, only G1's fields, in UTC, a 429 repeated as asked, a 403 never."""

import datetime as dt

import httpx
import pytest

from app.integrations.errors import (
    IntegrationRefusedError,
    IntegrationRejectedError,
    IntegrationUnavailableError,
    UnexpectedResponseError,
)
from app.integrations.graph import client as graph_client
from app.integrations.graph.client import EVENT_FIELDS, MAX_ATTEMPTS, MAX_RETRY_AFTER_SECONDS, GraphClient
from app.integrations.graph.config import GraphSettings
from app.integrations.graph.credential import TokenCache
from app.integrations.graph.models import CalendarEvent, EventType
from tests.integrations.graph.fake_graph import (
    CALENDARS,
    GRAPH_HOST,
    LOGIN_HOST,
    TOKEN_REQUEST,
    VIEW,
    VIEW_PATH,
    WINDOW,
    FakeGraph,
    answer,
    calendar_view,
    fixture,
    with_next_link,
)


async def read_window(settings: GraphSettings, fake: FakeGraph, tokens: TokenCache) -> list[CalendarEvent]:
    async with GraphClient.connect(settings, transport=fake.transport, tokens=tokens, sleep=fake.sleep) as client:
        return await client.calendar_view(*WINDOW)


def view_requests(fake: FakeGraph) -> list[httpx.Request]:
    return [request for request in fake.requests if request.url.path == VIEW_PATH]


def throttled(retry_after: str | None) -> httpx.Response:
    """Graph's 429: built here, as no fixture holds one."""
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return httpx.Response(429, json={"error": {"code": "TooManyRequests", "message": "Slow down"}}, headers=headers)


# --- paging and fields ---


async def test_paging_reads_to_the_end(settings, fake_graph: FakeGraph, tokens):
    events = await read_window(settings, fake_graph, tokens)

    pages = [fixture(f"g1-calendar-view-page-{n}")["response"]["body"]["value"] for n in (1, 2)]
    assert [event.id for event in events] == [item["id"] for page in pages for item in page]
    assert fake_graph.sent() == [
        f"POST {LOGIN_HOST}{TOKEN_REQUEST['path']}",
        f"GET {GRAPH_HOST}{CALENDARS['request']['path']}",
        f"GET {GRAPH_HOST}{VIEW_PATH}",
        f"GET {GRAPH_HOST}{VIEW_PATH}",
    ]
    second = view_requests(fake_graph)[1]
    next_link = fixture("g1-calendar-view-page-1")["response"]["body"]["@odata.nextLink"]
    assert second.url == httpx.URL(next_link)


async def test_only_the_fields_of_g1_are_requested(settings, fake_graph: FakeGraph, tokens):
    await read_window(settings, fake_graph, tokens)

    first = view_requests(fake_graph)[0]
    assert first.url.params["$select"] == VIEW["params"]["$select"] == ",".join(EVENT_FIELDS)
    assert {first.url.params[key] for key in ("startDateTime", "endDateTime")} == {
        VIEW["params"]["startDateTime"],
        VIEW["params"]["endDateTime"],
    }
    assert not {"body", "bodyPreview", "attendees", "organizer"} & set(EVENT_FIELDS)
    calendars = fake_graph.requests[1]
    assert calendars.url.params["$select"] == CALENDARS["request"]["params"]["$select"]


async def test_times_are_asked_for_and_read_in_utc(settings, fake_graph: FakeGraph, tokens):
    events = await read_window(settings, fake_graph, tokens)

    graph_requests = [request for request in fake_graph.requests if request.url.host == GRAPH_HOST]
    assert [request.headers["Prefer"] for request in graph_requests] == ['outlook.timezone="UTC"'] * 3
    first = events[0]
    assert first.start == dt.datetime(2026, 11, 16, 15, tzinfo=dt.UTC)
    assert first.end - first.start == dt.timedelta(hours=1)
    assert first.original_start == first.start


async def test_a_window_needs_aware_times(settings, fake_graph: FakeGraph, tokens):
    async with GraphClient.connect(settings, transport=fake_graph.transport, tokens=tokens) as client:
        with pytest.raises(ValueError, match="aware"):
            await client.calendar_view(dt.datetime(2026, 11, 16), dt.datetime(2026, 11, 17))
        with pytest.raises(ValueError, match="aware"):
            await client.calendar_view(WINDOW[1], WINDOW[0])

    assert view_requests(fake_graph) == []


# --- what an occurrence carries: G1 to G5 ---


@pytest.mark.parametrize(
    "name",
    [
        "g1-calendar-view-page-1",
        "g1-calendar-view-page-2",
        "g2-series-first-read",
        "g2-series-second-read",
        "g3-late-cancellation",
        "g4-special-entries",
        "g5-spring-clock-change",
        "g5-autumn-clock-change",
    ],
)
async def test_every_occurrence_of_the_fixtures_is_read(name: str, settings, fake_graph: FakeGraph, tokens):
    fake_graph.on("GET", VIEW_PATH, with_next_link(name, None))
    events = await read_window(settings, fake_graph, tokens)

    items = fixture(name)["response"]["body"]["value"]
    assert len(events) == len(items)
    for event, item in zip(events, items, strict=True):
        assert (event.id, event.type, event.series_master_id) == (item["id"], item["type"], item["seriesMasterId"])
        assert (event.subject, event.categories) == (item["subject"], tuple(item["categories"]))
        assert (event.is_all_day, event.is_cancelled) == (item["isAllDay"], item["isCancelled"])
        assert event.is_online_meeting == item["isOnlineMeeting"]
        assert event.location == item["location"]["displayName"]
        assert event.start.isoformat().startswith(item["start"]["dateTime"][:19])
        assert event.start.tzinfo == dt.UTC and event.end > event.start
        original = item.get("originalStart")
        assert event.original_start == (None if original is None else dt.datetime.fromisoformat(original))


async def test_the_special_entries_keep_what_the_import_decides_on(settings, fake_graph: FakeGraph, tokens):
    fake_graph.on("GET", VIEW_PATH, with_next_link("g4-special-entries", None))
    events = await read_window(settings, fake_graph, tokens)

    assert [event.is_all_day for event in events].count(True) == 1
    assert any(event.is_online_meeting for event in events)
    assert {event.type for event in events} == {EventType.SINGLE_INSTANCE}
    assert "Erika" not in repr(events) and "Kennenlernen" not in repr(events)


async def test_a_time_outside_utc_is_an_unexpected_response(settings, fake_graph: FakeGraph, tokens):
    body = fixture("g4-special-entries")["response"]["body"]
    item = {**body["value"][0], "start": {"dateTime": "2026-12-07T16:00:00.0000000", "timeZone": "W. Europe"}}
    fake_graph.on("GET", VIEW_PATH, httpx.Response(200, json={"value": [item]}))

    with pytest.raises(UnexpectedResponseError, match=r"\['start'\]") as raised:
        await read_window(settings, fake_graph, tokens)
    assert "2026-12-07" not in str(raised.value) and body["value"][0]["subject"] not in str(raised.value)


# --- a 429 is repeated as Retry-After says, a 403 never ---


async def test_a_429_is_repeated_as_retry_after_says(settings, fake_graph: FakeGraph, tokens):
    fake_graph.on("GET", VIEW_PATH, throttled("7"), throttled("2"), calendar_view)

    events = await read_window(settings, fake_graph, tokens)

    assert fake_graph.sleeps == [7, 2]
    assert len(view_requests(fake_graph)) == 4 and len(events) == 5


async def test_a_429_is_repeated_at_most_max_attempts_times(settings, fake_graph: FakeGraph, tokens):
    fake_graph.on("GET", VIEW_PATH, throttled("1"))

    with pytest.raises(IntegrationUnavailableError) as raised:
        await read_window(settings, fake_graph, tokens)

    assert raised.value.status == 429
    assert len(view_requests(fake_graph)) == MAX_ATTEMPTS
    assert fake_graph.sleeps == [1] * (MAX_ATTEMPTS - 1)


@pytest.mark.parametrize(
    "retry_after",
    [None, "", "soon", "-1", "1.5", "0x1", "Wed, 21 Oct 2026 07:28:00 GMT", str(MAX_RETRY_AFTER_SECONDS + 1)],
    ids=["missing", "empty", "a word", "negative", "a fraction", "hexadecimal", "a date", "above the cap"],
)
async def test_a_429_without_an_honoured_retry_after_is_not_repeated(
    retry_after, settings, fake_graph: FakeGraph, tokens
):
    fake_graph.on("GET", VIEW_PATH, throttled(retry_after), calendar_view)

    with pytest.raises(IntegrationUnavailableError, match="throttled"):
        await read_window(settings, fake_graph, tokens)

    assert len(view_requests(fake_graph)) == 1 and fake_graph.sleeps == []


async def test_the_cap_itself_is_honoured(settings, fake_graph: FakeGraph, tokens):
    fake_graph.on("GET", VIEW_PATH, throttled(str(MAX_RETRY_AFTER_SECONDS)), calendar_view)

    await read_window(settings, fake_graph, tokens)

    assert fake_graph.sleeps == [MAX_RETRY_AFTER_SECONDS]


async def test_a_403_is_not_repeated(settings, fake_graph: FakeGraph, tokens):
    fake_graph.on("GET", VIEW_PATH, answer("g6-mailbox-outside-scope"), calendar_view)

    with pytest.raises(IntegrationRejectedError) as raised:
        await read_window(settings, fake_graph, tokens)

    assert raised.value.status == 403 and raised.value.remote_message == "ErrorAccessDenied"
    assert len(view_requests(fake_graph)) == 1 and fake_graph.sleeps == []


async def test_a_server_error_is_not_repeated_either(settings, fake_graph: FakeGraph, tokens):
    fake_graph.on("GET", VIEW_PATH, httpx.Response(503, headers={"Retry-After": "1"}), calendar_view)

    with pytest.raises(IntegrationUnavailableError):
        await read_window(settings, fake_graph, tokens)

    assert len(view_requests(fake_graph)) == 1 and fake_graph.sleeps == []


# --- hosts ---


async def test_the_token_goes_to_the_login_host_and_every_read_to_graph(settings, fake_graph: FakeGraph, tokens):
    await read_window(settings, fake_graph, tokens)

    assert {(request.method, request.url.host) for request in fake_graph.requests} == {
        ("POST", LOGIN_HOST),
        ("GET", GRAPH_HOST),
    }
    token_request = fake_graph.requests[0]
    assert "authorization" not in token_request.headers
    assert all(request.headers["Authorization"].startswith("Bearer ") for request in fake_graph.requests[1:])


@pytest.mark.parametrize(
    "next_link",
    [
        f"https://{LOGIN_HOST}/v1.0/users/lessons@example.org/calendars",
        f"https://{GRAPH_HOST}.example.org/v1.0/users/lessons@example.org/calendars",
        "https://example.org/v1.0/users/lessons@example.org/calendars",
        f"http://{GRAPH_HOST}/v1.0/users/lessons@example.org/calendars",
        f"https://{GRAPH_HOST}:8443/v1.0/users/lessons@example.org/calendars",
        f"https://{GRAPH_HOST}/beta/users/lessons@example.org/calendars",
    ],
    ids=["the login host", "a lookalike host", "another host", "plain http", "another port", "another API version"],
)
async def test_a_next_link_off_the_graph_api_is_refused(next_link: str, settings, fake_graph: FakeGraph, tokens):
    fake_graph.on("GET", VIEW_PATH, with_next_link("g1-calendar-view-page-1", next_link))

    with pytest.raises(IntegrationRefusedError):
        await read_window(settings, fake_graph, tokens)

    assert len(fake_graph.requests) == 3
    assert all(request.url.host in {LOGIN_HOST, GRAPH_HOST} for request in fake_graph.requests)


async def test_a_listing_without_end_is_refused(settings, fake_graph: FakeGraph, tokens, monkeypatch):
    monkeypatch.setattr(graph_client, "MAX_PAGES", 3)
    looping = with_next_link("g1-calendar-view-page-1", f"https://{GRAPH_HOST}{VIEW_PATH}?$skip=1")
    fake_graph.on("GET", VIEW_PATH, looping)

    with pytest.raises(UnexpectedResponseError, match="more than 3 pages"):
        await read_window(settings, fake_graph, tokens)

    assert len(view_requests(fake_graph)) == 3
