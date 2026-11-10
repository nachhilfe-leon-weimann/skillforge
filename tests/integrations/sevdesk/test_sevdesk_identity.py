"""The identity check fails closed: no call but the identity read reaches an account that is not the configured one."""

import traceback

import httpx
import pytest

from app.integrations.config import IntegrationAccount
from app.integrations.errors import IdentityCheckError, IntegrationRefusedError
from app.integrations.sevdesk import client as sevdesk_client
from app.integrations.sevdesk.client import SevdeskClient
from app.integrations.sevdesk.config import SevdeskSettings
from tests.integrations.sevdesk.fake_sevdesk import ACCOUNT_ID, TOKEN, TRIAL_COMPANY, FakeSevdesk, objects


async def _connect_and_read(settings: SevdeskSettings, fake: FakeSevdesk) -> None:
    fake.on("GET", "/Tools/bookkeepingSystemVersion", objects({"version": "2.0"}))
    async with SevdeskClient.connect(settings, transport=fake.transport) as client:
        await client.bookkeeping_version()


async def test_the_configured_trial_account_passes_and_is_read_first(settings, fake_sevdesk: FakeSevdesk):
    await _connect_and_read(settings, fake_sevdesk)

    assert fake_sevdesk.sent() == ["GET /SevClient", "GET /Tools/bookkeepingSystemVersion"]


async def test_a_live_account_needs_no_trial_marker(settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/SevClient", objects([{"id": str(ACCOUNT_ID), "name": "Example Tutoring"}]))

    await _connect_and_read(settings.model_copy(update={"account": IntegrationAccount.LIVE}), fake_sevdesk)


ANSWERS = {
    "another account": objects([{"id": "4712", "name": TRIAL_COMPANY}]),
    "no trial marker": objects([{"id": str(ACCOUNT_ID), "name": "Example Tutoring"}]),
    "a lowercase marker": objects([{"id": str(ACCOUNT_ID), "name": "Example Tutoring trial"}]),
    "two accounts": objects([{"id": str(ACCOUNT_ID), "name": TRIAL_COMPANY}, {"id": "4712", "name": TRIAL_COMPANY}]),
    "no account": objects([]),
    "an unexpected body": objects([{"name": TRIAL_COMPANY}]),
    "not JSON": httpx.Response(200, text="<html>maintenance</html>"),
    "a server error": httpx.Response(500, json={"error": {"message": "Synthetic failure"}}),
    "an authentication error": httpx.Response(401, json={"error": {"message": "Authentication required"}}),
    "a transport error": httpx.ConnectError("refused"),
    "a timeout": httpx.ReadTimeout("timed out"),
}


@pytest.mark.parametrize("answer", ANSWERS.values(), ids=ANSWERS.keys())
async def test_the_check_fails_closed(answer, settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/SevClient", answer)

    with pytest.raises(IdentityCheckError):
        await _connect_and_read(settings, fake_sevdesk)

    assert fake_sevdesk.sent() == ["GET /SevClient"]


async def test_a_missing_expected_account_id_sends_nothing(settings, fake_sevdesk: FakeSevdesk):
    with pytest.raises(IdentityCheckError, match="EXPECTED_ACCOUNT_ID"):
        await _connect_and_read(settings.model_copy(update={"expected_account_id": None}), fake_sevdesk)

    assert fake_sevdesk.requests == []


async def test_a_missing_token_sends_nothing(settings, fake_sevdesk: FakeSevdesk):
    with pytest.raises(IntegrationRefusedError, match="TOKEN"):
        await _connect_and_read(settings.model_copy(update={"token": None}), fake_sevdesk)

    assert fake_sevdesk.requests == []


async def test_the_messages_name_account_ids_but_never_the_company(settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/SevClient", objects([{"id": "4712", "name": "Example Tutoring Company"}]))

    with pytest.raises(IdentityCheckError) as raised:
        await _connect_and_read(settings, fake_sevdesk)

    assert str(raised.value) == f"the token belongs to sevDesk account 4712, not to {ACCOUNT_ID}"
    assert "Example" not in repr(raised.value)


async def test_a_client_not_built_by_connect_sends_nothing_but_the_identity_read(settings, fake_sevdesk):
    async with sevdesk_client._sevdesk_http(settings, transport=fake_sevdesk.transport) as http:
        client = SevdeskClient(http, settings)
        for call in (client.get_contact(21), client.parts(), client.bookkeeping_version()):
            with pytest.raises(IdentityCheckError, match="not confirmed"):
                await call
        assert fake_sevdesk.requests == []

        assert (await client.account()).id == ACCOUNT_ID

    assert fake_sevdesk.sent() == ["GET /SevClient"]


LEAKY_FAILURES = {
    "a rejection": (
        httpx.Response(401, json={"error": {"message": f"Synthetic detail {TOKEN}"}}),
        "IntegrationRejectedError) (HTTP 401",
    ),
    "a protocol error": (httpx.LocalProtocolError(f"Illegal header value b'{TOKEN} '"), "IntegrationUnavailableError"),
}


@pytest.mark.parametrize(("answer", "failure"), LEAKY_FAILURES.values(), ids=LEAKY_FAILURES.keys())
async def test_a_failed_identity_read_leaks_nothing_into_its_traceback(
    answer, failure: str, settings, fake_sevdesk: FakeSevdesk
):
    fake_sevdesk.on("GET", "/SevClient", answer)

    with pytest.raises(IdentityCheckError) as raised:
        await _connect_and_read(settings, fake_sevdesk)

    text = "".join(traceback.format_exception(raised.value))
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert TOKEN not in text and "Synthetic detail" not in text
    assert str(raised.value) == f"the sevDesk account could not be read ({failure})"
