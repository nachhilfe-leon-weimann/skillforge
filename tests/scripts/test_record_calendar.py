"""The calendar spike recorder (``scripts/graph/record_calendar.py``) with synthetic data and mocked HTTP only.

No test reaches the network: every Graph request goes to an ``httpx.MockTransport`` and the token comes from a
stub, so ``msal`` is never imported.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import stat
import sys
import types
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts.graph import record_calendar as rc

SALT = bytes(range(32))
MAILBOX = "calendar@example.org"
OTHER_MAILBOX = "someone-else@example.org"
TENANT_ID = "00000000-0000-0000-0000-00000000aaaa"
CLIENT_ID = "00000000-0000-0000-0000-00000000bbbb"
CALENDAR_NAME = "Unterricht"
CALENDAR_ID = "synthetic-calendar-id"

SYNTHETIC_NAMES = ["Erika Mustermann", "Max Mustermann", "Anna-Lena Beispiel", "Jörg Testmann", "Erika Musterman"]
NAME_TOKENS = {token.casefold() for name in SYNTHETIC_NAMES for token in re.findall(r"[^\W_]+", name)}

Handler = Callable[[httpx.Request], httpx.Response]


def _event(event_id: str, subject: str, start: str, end: str, **fields: Any) -> dict[str, Any]:
    return {
        "@odata.etag": 'W/"synthetic"',
        "id": event_id,
        "iCalUId": f"ical-{event_id}",
        "seriesMasterId": None,
        "type": "singleInstance",
        "subject": subject,
        "start": {"dateTime": f"{start}.0000000", "timeZone": "UTC"},
        "end": {"dateTime": f"{end}.0000000", "timeZone": "UTC"},
        "isAllDay": False,
        "isCancelled": False,
        "isOnlineMeeting": False,
        "onlineMeetingProvider": "unknown",
        "location": {"displayName": "", "locationType": "default"},
        "categories": [],
        "showAs": "busy",
        "lastModifiedDateTime": "2026-03-01T09:30:00.1234567Z",
    } | fields


def _graph_error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": f"Access to {MAILBOX} denied"}})


def _client(handler: Handler, sleeps: list[float] | None = None) -> rc.GraphClient:
    return rc.GraphClient(
        "synthetic-token",
        transport=httpx.MockTransport(handler),
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
    )


# region Pseudonymization


def test_name_pseudonyms_are_stable_across_runs_and_keep_the_shape() -> None:
    first, second = rc.Pseudonymizer(SALT), rc.Pseudonymizer(SALT)

    pseudonym = first.pseudo_name("Anna-Lena Beispiel")

    shape = re.fullmatch(r"([A-Z][a-z]+)-([A-Z][a-z]+) ([A-Z][a-z]+)", pseudonym)
    assert shape is not None
    assert pseudonym == second.pseudo_name("Anna-Lena Beispiel")
    anna, lena, beispiel = shape.groups()
    assert first.pseudo_name("anna-lena BEISPIEL") == f"{anna.lower()}-{lena.lower()} {beispiel.upper()}"
    assert rc.Pseudonymizer(bytes(32)).pseudo_name("Anna-Lena Beispiel") != pseudonym


def test_name_tokens_map_per_normalised_token_and_typos_stay_distinct() -> None:
    pseudonymizer = rc.Pseudonymizer(SALT)

    erika, mustermann = pseudonymizer.pseudo_name("Erika Mustermann").split()

    assert pseudonymizer.pseudo_name("Max Mustermann").split()[1] == mustermann
    assert pseudonymizer.pseudo_name("Erika Musterman").split() == [erika, pseudonymizer.pseudo_name("Musterman")]
    assert pseudonymizer.pseudo_name("Musterman") != mustermann
    assert pseudonymizer.pseudo_name("Jörg") == pseudonymizer.pseudo_name("Joerg")
    max_, initial, digit = (pseudonymizer.pseudo_name(word) for word in ("Max", "M", "2"))
    assert pseudonymizer.pseudo_name("Max M. 2") == f"{max_} {initial}. {digit}"


def test_initials_and_digits_keep_their_class() -> None:
    pseudonymizer = rc.Pseudonymizer(SALT)

    initial, digits = pseudonymizer.pseudo_name("M. 4711").split()

    assert re.fullmatch(r"[A-Z]\.", initial)
    assert re.fullmatch(r"\d{4}", digits)


@pytest.mark.parametrize(
    ("subject", "prefix"),
    [
        ("Mathe mit Erika Mustermann", "Mathe"),
        ("Kennenlernen mit Max Mustermann", "Kennenlernen"),
        ("Abgesagt: Physik mit Jörg Testmann", "Abgesagt: Physik"),
        ("Mathe/Physik mit Anna-Lena Beispiel", "Mathe/Physik"),
        ("Klavier mit Erika Mustermann", "[subject]"),
        ("Mathe 10 mit Erika Mustermann", "[subject]"),
        ("Erika mit Max Mustermann", "[subject]"),
    ],
)
def test_subject_prefix_survives_only_on_the_allowlist(subject: str, prefix: str) -> None:
    pseudonym = rc.Pseudonymizer(SALT).pseudo_subject(subject)

    assert rc.subject_prefix(pseudonym) == prefix
    assert not NAME_TOKENS & {token.casefold() for token in re.findall(r"[^\W_]+", pseudonym)}


def test_extra_subjects_extend_the_allowlist() -> None:
    pseudonymizer = rc.Pseudonymizer(SALT, ["Klavier", "Deutsch als Fremdsprache"])

    assert pseudonymizer.pseudo_subject("Klavier mit Erika Mustermann").startswith("Klavier mit ")
    assert pseudonymizer.pseudo_subject("Deutsch als Fremdsprache mit Max Mustermann").startswith("Deutsch als ")


@pytest.mark.parametrize("subject", ["Zahnarzt", "Termin Erika Mustermann", "", None, "Mathe mit ", "Mathematik"])
def test_titles_without_a_name_become_the_unmatched_placeholder(subject: str | None) -> None:
    assert rc.Pseudonymizer(SALT).pseudo_subject(subject) == "[unmatched]"


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ({"displayName": "", "locationType": "default"}, "empty"),
        (None, "empty"),
        ({"displayName": "+49 170 0000000"}, "phone"),
        ({"displayName": "0170/000 00 00"}, "phone"),
        ({"displayName": "Microsoft Teams-Besprechung"}, "teams"),
        ({"displayName": "Online", "locationUri": "https://teams.microsoft.com/l/meetup-join/synthetic"}, "teams"),
        ({"displayName": "https://meet.example.org/synthetic"}, "url"),
        ({"displayName": "Musterweg 1, 12345 Musterstadt", "address": {"street": "Musterweg 1"}}, "other"),
        ({"displayName": "Raum 12"}, "other"),
    ],
)
def test_locations_are_replaced_by_a_synthetic_value_of_the_same_class(location: Any, expected: str) -> None:
    event = rc.Pseudonymizer(SALT).event(
        _event("e1", "Mathe mit Max Mustermann", "2026-03-25T15:00:00", "2026-03-25T16:00:00", location=location)
    )

    assert rc.classify_location(location) == expected
    assert event["location"]["displayName"] == rc.SYNTHETIC_LOCATIONS[expected]
    assert rc.classify_location(event["location"]) == expected
    assert set(event["location"]) == {"displayName", "locationType"}


def test_ids_are_keyed_hashes_that_preserve_equality() -> None:
    pseudonymizer = rc.Pseudonymizer(SALT)
    occurrence = pseudonymizer.event(
        _event("occ-1", "Mathe mit Max Mustermann", "2026-03-25T15:00:00", "2026-03-25T16:00:00", seriesMasterId="m-1")
    )

    assert occurrence["seriesMasterId"] == pseudonymizer.pseudo_id("m-1")
    assert occurrence["id"] == rc.Pseudonymizer(SALT).pseudo_id("occ-1") != pseudonymizer.pseudo_id("occ-2")
    assert occurrence["id"] != "occ-1"
    assert occurrence["iCalUId"] == pseudonymizer.pseudo_id("ical-occ-1")
    assert rc.Pseudonymizer(bytes(32)).pseudo_id("occ-1") != occurrence["id"]


def test_events_keep_only_the_selected_fields() -> None:
    raw = _event("e1", "Mathe mit Max Mustermann", "2026-03-25T15:00:00", "2026-03-25T16:00:00")
    raw |= {"body": {"content": "Max Mustermann"}, "attendees": [{"emailAddress": {"address": MAILBOX}}]}

    event = rc.Pseudonymizer(SALT).event(raw)

    assert list(event) == list(rc.EVENT_FIELDS)
    assert "Mustermann" not in json.dumps(event)


# endregion


# region Dates


@pytest.mark.parametrize(
    ("utc", "shifted"),
    [
        # 16:00 CET on the Wednesday before the spring change -> 16:00 CEST a week later.
        ("2026-03-25T15:00:00+00:00", "2026-04-01T14:00:00+00:00"),
        # 16:00 CEST on the Wednesday before the autumn change -> 16:00 CET a week later.
        ("2026-10-21T14:00:00+00:00", "2026-10-28T15:00:00+00:00"),
    ],
)
def test_a_shift_keeps_the_berlin_wall_clock_across_dst(utc: str, shifted: str) -> None:
    assert rc._shift_local(datetime.fromisoformat(utc), 1) == datetime.fromisoformat(shifted)


@pytest.mark.parametrize("start", ["2026-03-27T15:00:00", "2026-10-23T14:00:00", "2026-07-01T06:30:00"])
def test_the_secret_offset_is_whole_weeks_and_keeps_weekday_and_local_time(start: str) -> None:
    pseudonymizer = rc.Pseudonymizer(SALT)
    original = datetime.fromisoformat(start).replace(tzinfo=UTC)

    shifted = pseudonymizer.shift_datetime_tz({"dateTime": f"{start}.0000000", "timeZone": "UTC"}, all_day=False)
    moved = datetime.fromisoformat(shifted["dateTime"]).replace(tzinfo=UTC)

    assert 52 <= abs(pseudonymizer.week_offset) <= 520
    assert pseudonymizer.week_offset == rc.Pseudonymizer(SALT).week_offset
    local_before, local_after = original.astimezone(rc.LOCAL_TZ), moved.astimezone(rc.LOCAL_TZ)
    assert (local_after.weekday(), local_after.time()) == (local_before.weekday(), local_before.time())
    assert (local_after.date() - local_before.date()).days == 7 * pseudonymizer.week_offset


def test_all_day_events_stay_floating_midnights() -> None:
    pseudonymizer = rc.Pseudonymizer(SALT)

    shifted = pseudonymizer.shift_datetime_tz(
        {"dateTime": "2026-03-29T00:00:00.0000000", "timeZone": "UTC"}, all_day=True
    )

    assert shifted["dateTime"].endswith("T00:00:00.0000000")


# endregion


# region Microsoft Graph


def test_paging_follows_next_links_without_repeating_the_query() -> None:
    requests: list[httpx.Request] = []
    next_link = f"{rc.GRAPH_BASE_URL}/users/{MAILBOX}/calendars/{CALENDAR_ID}/calendarView?$skiptoken=page2"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "skiptoken" in str(request.url):
            return httpx.Response(200, json={"value": [{"id": "b"}]})
        return httpx.Response(200, json={"value": [{"id": "a"}], "@odata.nextLink": next_link})

    with _client(handler) as client:
        events = rc.fetch_events(client, MAILBOX, CALENDAR_ID, date(2026, 3, 1), date(2026, 3, 31))

    assert [e["id"] for e in events] == ["a", "b"]
    first, second = requests
    assert first.url.params["$select"] == ",".join(rc.EVENT_FIELDS)
    assert first.url.params["startDateTime"] == "2026-02-28T23:00:00Z"
    assert first.url.params["endDateTime"] == "2026-03-31T22:00:00Z"
    assert first.headers["Prefer"] == 'outlook.timezone="UTC"'
    assert str(second.url) == next_link


def test_a_next_link_outside_graph_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [], "@odata.nextLink": "https://example.org/steal"})

    with _client(handler) as client, pytest.raises(rc.RecorderError, match="outside Microsoft Graph"):
        list(client.iter_values(f"{rc.GRAPH_BASE_URL}/users/{MAILBOX}/calendars"))


def test_throttling_waits_for_retry_after_capped_then_succeeds() -> None:
    responses = iter([
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(429, headers={"Retry-After": "86400"}),
        httpx.Response(429),
        httpx.Response(200, json={"value": []}),
    ])
    sleeps: list[float] = []

    with _client(lambda _: next(responses), sleeps) as client:
        assert client.get_json(f"{rc.GRAPH_BASE_URL}/users/{MAILBOX}/calendars") == {"value": []}

    assert sleeps == [7.0, rc.MAX_RETRY_AFTER_SECONDS, rc.DEFAULT_RETRY_AFTER_SECONDS]


def test_throttling_is_bounded() -> None:
    sleeps: list[float] = []

    with _client(lambda _: httpx.Response(429, headers={"Retry-After": "1"}), sleeps) as client:
        with pytest.raises(rc.GraphError, match="429"):
            client.get_json(f"{rc.GRAPH_BASE_URL}/users/{MAILBOX}/calendars")

    assert len(sleeps) == rc.MAX_RETRIES


def test_graph_errors_report_the_code_but_not_the_message() -> None:
    with _client(lambda _: _graph_error(403, "ErrorAccessDenied")) as client:
        with pytest.raises(rc.GraphError) as raised:
            client.get_json(f"{rc.GRAPH_BASE_URL}/users/{MAILBOX}/calendars")

    assert str(raised.value) == "Microsoft Graph answered 403 (ErrorAccessDenied)"


def test_transport_errors_do_not_echo_the_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    with _client(handler) as client, pytest.raises(rc.RecorderError) as raised:
        client.get_json(f"{rc.GRAPH_BASE_URL}/users/{MAILBOX}/calendars")

    assert str(raised.value) == "request to Microsoft Graph failed: ConnectError"


@pytest.mark.parametrize(
    ("response", "restricted"),
    [
        (_graph_error(403, "ErrorAccessDenied"), "yes"),
        (httpx.Response(200, json={"value": [{"id": "x"}]}), "no"),
        (_graph_error(404, "ErrorInvalidUser"), "inconclusive (HTTP 404)"),
    ],
)
def test_the_probe_reads_403_as_restricted(response: httpx.Response, restricted: str) -> None:
    with _client(lambda _: response) as client:
        assert rc.probe_mailbox(client, OTHER_MAILBOX) == restricted


def test_the_calendar_is_found_by_name() -> None:
    calendars = {"value": [{"id": "other", "name": "Kalender"}, {"id": CALENDAR_ID, "name": CALENDAR_NAME}]}

    with _client(lambda _: httpx.Response(200, json=calendars)) as client:
        assert rc.find_calendar_id(client, MAILBOX, CALENDAR_NAME.upper()) == CALENDAR_ID
        with pytest.raises(rc.RecorderError, match="none of the 2"):
            rc.find_calendar_id(client, MAILBOX, "Privat")


# endregion


# region Config, salt and output


def test_the_certificate_is_read_from_a_pem_file_and_its_thumbprint_is_the_sha1_of_its_der() -> None:
    der = b"synthetic certificate bytes"
    body = base64.encodebytes(der).decode()
    pem = (
        "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"
        f"-----BEGIN CERTIFICATE-----\n{body}-----END CERTIFICATE-----\n"
    )

    assert rc.certificate_pem(pem) == f"-----BEGIN CERTIFICATE-----\n{body}-----END CERTIFICATE-----"
    assert rc.certificate_thumbprint(pem) == hashlib.sha1(der).hexdigest().upper()


@pytest.mark.parametrize("text", ["", "-----BEGIN CERTIFICATE-----\nnot base64!\n-----END CERTIFICATE-----"])
def test_a_file_without_a_certificate_is_refused(text: str) -> None:
    with pytest.raises(rc.RecorderError, match="no PEM certificate"):
        rc.certificate_pem(text)


class _StubApp:
    """Stands in for ``msal.ConfidentialClientApplication``; records what the script hands over."""

    calls: list[dict[str, Any]] = []
    result: dict[str, Any] = {}

    def __init__(self, client_id: str, *, authority: str, client_credential: dict[str, str]) -> None:
        self.calls.append({"client_id": client_id, "authority": authority, "credential": client_credential})

    def acquire_token_for_client(self, scopes: list[str]) -> dict[str, Any]:
        self.calls.append({"scopes": scopes})
        return self.result


def _config(tmp_path: Path) -> rc.GraphConfig:
    body = base64.encodebytes(b"synthetic certificate").decode()
    (tmp_path / "cert.pem").write_text(f"-----BEGIN CERTIFICATE-----\n{body}-----END CERTIFICATE-----\n")
    (tmp_path / "key.pem").write_text("synthetic key")
    return rc.GraphConfig(
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        certificate_path=tmp_path / "cert.pem",
        private_key_path=tmp_path / "key.pem",
        mailbox=MAILBOX,
        calendar_name=CALENDAR_NAME,
    )


@pytest.fixture
def stub_msal(monkeypatch: pytest.MonkeyPatch) -> type[_StubApp]:
    monkeypatch.setitem(sys.modules, "msal", types.SimpleNamespace(ConfidentialClientApplication=_StubApp))
    monkeypatch.setattr(_StubApp, "calls", [])
    return _StubApp


def test_the_token_request_hands_msal_the_certificate_for_a_sha256_thumbprint(
    tmp_path: Path, stub_msal: type[_StubApp], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stub_msal, "result", {"access_token": "synthetic-token"})

    assert rc.acquire_token(_config(tmp_path)) == "synthetic-token"

    construction, request = stub_msal.calls
    assert construction["authority"] == f"https://login.microsoftonline.com/{TENANT_ID}"
    assert construction["credential"] == {
        "private_key": "synthetic key",
        "public_certificate": rc.certificate_pem((tmp_path / "cert.pem").read_text()),
    }
    assert request == {"scopes": ["https://graph.microsoft.com/.default"]}


def test_a_refused_token_reports_codes_and_thumbprint_but_not_the_description(
    tmp_path: Path, stub_msal: type[_StubApp], monkeypatch: pytest.MonkeyPatch
) -> None:
    description = f"AADSTS700027: synthetic description naming tenant {TENANT_ID}"
    monkeypatch.setattr(
        stub_msal, "result", {"error": "invalid_client", "error_codes": [700027], "error_description": description}
    )

    with pytest.raises(rc.RecorderError) as raised:
        rc.acquire_token(_config(tmp_path))

    thumbprint = hashlib.sha1(b"synthetic certificate").hexdigest().upper()
    assert str(raised.value) == (
        f"token request failed: invalid_client 700027; the app registration must list the certificate {thumbprint}"
    )


def test_a_missing_key_is_named_without_echoing_values(tmp_path: Path) -> None:
    env_file = tmp_path / "graph.env"
    env_file.write_text(f'GRAPH__TENANT_ID="{TENANT_ID}"\nexport GRAPH__MAILBOX={MAILBOX}\n')

    with pytest.raises(rc.RecorderError) as raised:
        rc.load_config(env_file)

    assert "GRAPH__CLIENT_ID" in str(raised.value)
    assert TENANT_ID not in str(raised.value) and MAILBOX not in str(raised.value)


def test_the_salt_is_created_once_with_mode_600(tmp_path: Path) -> None:
    path = tmp_path / "config" / "graph-pseudonym.salt"

    salt = rc.load_or_create_salt(path)

    assert len(salt) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert rc.load_or_create_salt(path) == salt


def test_out_inside_tests_is_refused() -> None:
    with pytest.raises(rc.RecorderError, match="tests/"):
        rc.check_out_dir(rc.REPO_ROOT / "tests" / "fixtures", force=True)


def test_an_existing_fixture_needs_force(tmp_path: Path) -> None:
    (tmp_path / rc.FIXTURE_NAME).write_text("{}")

    with pytest.raises(rc.RecorderError, match="--force"):
        rc.check_out_dir(tmp_path, force=False)
    assert rc.check_out_dir(tmp_path, force=True) == (tmp_path / rc.FIXTURE_NAME).resolve()


# endregion


# region End to end


RAW_EVENTS = [
    _event(
        "e1",
        "Mathe mit Erika Mustermann",
        "2026-03-24T15:00:00",
        "2026-03-24T16:00:00",
        type="occurrence",
        seriesMasterId="m1",
    ),
    _event(
        "e2",
        "Mathe mit Erika Mustermann",
        "2026-03-31T14:00:00",
        "2026-03-31T15:00:00",
        type="occurrence",
        seriesMasterId="m1",
    ),
    _event(
        "e3", "Englisch mit Max Mustermann", "2026-03-25T16:00:00", "2026-03-25T17:30:00", categories=["Spät abgesagt"]
    ),
    _event(
        "e4",
        "Physik mit Anna-Lena Beispiel",
        "2026-03-26T17:00:00",
        "2026-03-26T17:45:00",
        isOnlineMeeting=True,
        onlineMeetingProvider="teamsForBusiness",
        location={"displayName": "Microsoft Teams-Besprechung", "locationType": "default"},
    ),
    _event(
        "e5",
        "Kennenlernen mit Jörg Testmann",
        "2026-03-27T16:00:00",
        "2026-03-27T16:30:00",
        location={"displayName": "+49 170 0000000", "locationType": "default"},
    ),
    _event("e6", "Mathe mit Erika Musterman", "2026-03-28T09:00:00", "2026-03-28T10:00:00", isCancelled=True),
    _event(
        "e7",
        "Zahnarzt Erika Mustermann",
        "2026-03-30T07:00:00",
        "2026-03-30T08:00:00",
        location={"displayName": "Musterweg 1, 12345 Musterstadt", "locationType": "streetAddress"},
    ),
    _event("e8", "Urlaub", "2026-03-29T00:00:00", "2026-03-30T00:00:00", isAllDay=True, showAs="oof"),
]


def _graph_handler(calls: list[str]) -> Handler:
    throttled = iter([True])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        assert request.url.host == "graph.microsoft.com"
        if path == f"/v1.0/users/{OTHER_MAILBOX}/calendars":
            return _graph_error(403, "ErrorAccessDenied")
        if path == f"/v1.0/users/{MAILBOX}/calendars":
            return httpx.Response(200, json={"value": [{"id": CALENDAR_ID, "name": CALENDAR_NAME}]})
        assert path == f"/v1.0/users/{MAILBOX}/calendars/{CALENDAR_ID}/calendarView"
        if next(throttled, False):
            return httpx.Response(429, headers={"Retry-After": "0"})
        if "$skiptoken" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "value": RAW_EVENTS[:5],
                    "@odata.nextLink": f"{request.url.copy_with(params={'$skiptoken': '2'})}",
                },
            )
        return httpx.Response(200, json={"value": RAW_EVENTS[5:]})

    return handler


def test_record_writes_a_fixture_and_summary_free_of_the_synthetic_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("cert.pem", "key.pem"):
        (tmp_path / name).write_text("synthetic")
    env_file = tmp_path / "graph.env"
    env_file.write_text(
        "\n".join([
            f"GRAPH__TENANT_ID={TENANT_ID}",
            f"GRAPH__CLIENT_ID={CLIENT_ID}",
            f"GRAPH__CERTIFICATE_PATH={tmp_path / 'cert.pem'}",
            f"GRAPH__PRIVATE_KEY_PATH={tmp_path / 'key.pem'}",
            f"GRAPH__MAILBOX={MAILBOX}",
            f"GRAPH__CALENDAR_NAME={CALENDAR_NAME}",
        ])
    )
    salt_file = tmp_path / "salt"
    salt_file.write_text(SALT.hex())
    salt_file.chmod(0o600)
    out = tmp_path / "out"
    calls: list[str] = []

    status = rc.main(
        [
            *("--env-file", str(env_file), "--start", "2026-03-23", "--end", "2026-03-31"),
            *("--out", str(out), "--probe-mailbox", OTHER_MAILBOX, "--salt-file", str(salt_file)),
        ],
        token_provider=lambda config: "synthetic-token",
        transport=httpx.MockTransport(_graph_handler(calls)),
        sleep=lambda _: None,
    )

    summary = capsys.readouterr().out
    fixture_text = (out / rc.FIXTURE_NAME).read_text()
    fixture = json.loads(fixture_text)
    assert status == 0
    assert len(calls) == 5  # calendars, calendarView throttled + two pages, the probe
    assert stat.S_IMODE((out / rc.FIXTURE_NAME).stat().st_mode) == 0o600
    assert len(fixture["value"]) == len(RAW_EVENTS)

    for text in (summary, fixture_text):
        words = {word.casefold() for word in re.findall(r"[^\W_]+", text)}
        assert not NAME_TOKENS & words
        for secret in (
            MAILBOX,
            OTHER_MAILBOX,
            TENANT_ID,
            CLIENT_ID,
            CALENDAR_NAME,
            CALENDAR_ID,
            "Musterweg",
            "+49 170",
        ):
            assert secret not in text
        for raw in RAW_EVENTS:
            assert raw["id"] not in re.findall(r'"([^"]+)"', text)
            assert raw["start"]["dateTime"][:10] not in text
    assert not re.search(r"\d{4}-\d{2}-\d{2}", summary)

    assert "  lessons (<subject> mit <name>): 5" in summary
    assert "  kennenlernen: 1" in summary
    assert "  unmatched titles: 2" in summary
    assert "cancelled (isCancelled): 1" in summary
    assert "all-day: 1" in summary
    assert "  teamsForBusiness: 1" in summary
    assert "  Spät abgesagt: 1" in summary
    assert "  Mathe: 3" in summary
    assert "  60: 4" in summary
    assert "probe of another mailbox: restricted: yes" in summary


def test_record_refuses_tests_before_any_request(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    status = rc.main(
        [
            *("--env-file", str(tmp_path / "missing.env"), "--start", "2026-03-01", "--end", "2026-03-31"),
            *("--out", str(rc.REPO_ROOT / "tests" / "fixtures"), "--salt-file", str(tmp_path / "salt")),
        ],
        token_provider=lambda config: pytest.fail("no token expected"),
        transport=httpx.MockTransport(handler),
    )

    assert status == 1
    assert not (tmp_path / "salt").exists()


# endregion
