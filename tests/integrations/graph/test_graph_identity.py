"""The identity check fails closed: no calendar is read before the lesson calendar is confirmed in the mailbox."""

import copy

import httpx
import pytest

from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.errors import IdentityCheckError, IntegrationRefusedError
from app.integrations.graph.client import GraphClient
from app.integrations.graph.config import GraphSettings
from app.integrations.graph.credential import TokenCache
from app.integrations.transport import IntegrationHttp
from tests.integrations.graph.fake_graph import (
    CALENDAR_ID,
    CALENDAR_NAME,
    CALENDARS,
    GRAPH_HOST,
    LOGIN_HOST,
    MAILBOX,
    TOKEN_REQUEST,
    VIEW_PATH,
    WINDOW,
    FakeGraph,
    answer,
)

TOKEN = f"POST {LOGIN_HOST}{TOKEN_REQUEST['path']}"
IDENTITY = f"GET {GRAPH_HOST}{CALENDARS['request']['path']}"
VIEW = f"GET {GRAPH_HOST}{VIEW_PATH}"


async def read(settings: GraphSettings, fake: FakeGraph, tokens: TokenCache) -> None:
    async with GraphClient.connect(settings, transport=fake.transport, tokens=tokens, sleep=fake.sleep) as client:
        await client.calendar_view(*WINDOW)


def calendars(change) -> httpx.Response:
    """G1's calendars with ``change`` applied to the lesson calendar."""
    body = copy.deepcopy(CALENDARS["response"]["body"])
    lesson = next(item for item in body["value"] if item["name"] == CALENDAR_NAME)
    change(body, lesson)
    return httpx.Response(200, json=body)


async def test_the_calendar_is_confirmed_before_it_is_read(settings, fake_graph: FakeGraph, tokens):
    await read(settings, fake_graph, tokens)

    assert fake_graph.sent() == [TOKEN, IDENTITY, VIEW, VIEW]
    assert CALENDAR_ID in fake_graph.requests[2].url.path


async def test_an_owner_address_in_other_letter_case_is_the_same_mailbox(settings, fake_graph: FakeGraph, tokens):
    fake_graph.on(
        "GET", CALENDARS["request"]["path"], calendars(lambda _, c: c["owner"].update(address=MAILBOX.upper()))
    )

    await read(settings, fake_graph, tokens)

    assert fake_graph.sent()[2] == VIEW


ANSWERS = {
    "a calendar owned by another address": calendars(
        lambda _, c: c["owner"].update(address="someone-else@example.org")
    ),
    "an owner address that contains the mailbox": calendars(lambda _, c: c["owner"].update(address=f"other-{MAILBOX}")),
    "an owner address inside the mailbox's": calendars(lambda _, c: c["owner"].update(address=MAILBOX[1:])),
    "a missing calendar": calendars(lambda body, c: body["value"].remove(c)),
    "a renamed calendar": calendars(lambda _, c: c.update(name=f"{CALENDAR_NAME} alt")),
    "two calendars of that name": calendars(
        lambda body, c: body["value"].append({**c, "id": "AAMkAGc3ludGhldGljOnR3bw=="})
    ),
    "a calendar without an owner": calendars(lambda _, c: c.pop("owner")),
    "an owner without an address": calendars(lambda _, c: c.update(owner={"name": "Example Tutoring"})),
    "an owner with an empty address": calendars(lambda _, c: c.update(owner={"address": ""})),
    "a mailbox outside the restriction (G6)": answer("g6-mailbox-outside-scope"),
    "a server error": httpx.Response(500, json={"error": {"code": "generalException"}}),
    "a throttled read without Retry-After": httpx.Response(429),
    "not JSON": httpx.Response(200, text="<html>maintenance</html>"),
    "an unexpected body": httpx.Response(200, json={"value": [{"name": CALENDAR_NAME}]}),
    "a transport error": httpx.ConnectError("refused"),
}


@pytest.mark.parametrize("answer_", ANSWERS.values(), ids=ANSWERS.keys())
async def test_the_check_fails_closed(answer_, settings, fake_graph: FakeGraph, tokens):
    fake_graph.on("GET", CALENDARS["request"]["path"], answer_)

    with pytest.raises(IdentityCheckError) as raised:
        await read(settings, fake_graph, tokens)

    assert fake_graph.sent() == [TOKEN, IDENTITY]
    assert MAILBOX not in str(raised.value) and CALENDAR_NAME not in str(raised.value)


async def test_a_calendar_list_that_points_off_graph_fails_closed(settings, fake_graph: FakeGraph, tokens):
    off_host = calendars(lambda body, _: body.update({"@odata.nextLink": "https://example.org/v1.0/users"}))
    fake_graph.on("GET", CALENDARS["request"]["path"], off_host)

    with pytest.raises(IdentityCheckError):
        await read(settings, fake_graph, tokens)

    assert fake_graph.sent() == [TOKEN, IDENTITY]


@pytest.mark.parametrize("unset", ["mailbox", "calendar_name"])
async def test_a_missing_mailbox_or_calendar_name_sends_nothing(unset: str, settings, fake_graph: FakeGraph, tokens):
    with pytest.raises(IdentityCheckError, match="GRAPH__MAILBOX or GRAPH__CALENDAR_NAME"):
        await read(settings.model_copy(update={unset: None}), fake_graph, tokens)

    assert fake_graph.requests == []


@pytest.mark.parametrize("unset", ["tenant_id", "client_id", "certificate_pem", "private_key_pem"])
async def test_a_missing_credential_sends_nothing(unset: str, settings, fake_graph: FakeGraph, tokens):
    with pytest.raises(IntegrationRefusedError, match=f"GRAPH__{unset.upper()}"):
        await read(settings.model_copy(update={unset: None}), fake_graph, tokens)

    assert fake_graph.requests == []


async def test_a_client_that_was_not_connected_reads_no_calendar(fake_graph: FakeGraph):
    http = IntegrationHttp(
        name="graph",
        base_url=f"https://{GRAPH_HOST}/v1.0",
        allowed_hosts=frozenset({GRAPH_HOST}),
        mode=IntegrationMode.DRY_RUN,
        account=IntegrationAccount.LIVE,
        transport=fake_graph.transport,
    )
    async with http:
        with pytest.raises(IdentityCheckError, match="not confirmed"):
            await GraphClient(http, mailbox=MAILBOX).calendar_view(*WINDOW)

    assert fake_graph.requests == []
