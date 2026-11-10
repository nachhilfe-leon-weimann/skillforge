# /// script
# requires-python = ">=3.14"
# dependencies = ["httpx>=0.28,<1", "msal>=1.35,<2"]
# ///
"""Record the lesson calendar as a pseudonymized fixture for the calendar-import slice.

Only the maintainer runs this, on the maintainer's machine; agents never run it against Microsoft Graph and
never see raw calendar data. It reads one calendar of one mailbox through Microsoft Graph with app-only client
credentials (a certificate, no secret), pseudonymizes every event in memory and only then writes a JSON
fixture and prints a summary that is safe to paste into an agent chat:

    uv run scripts/graph/record_calendar.py --env-file ~/.config/skillforge/graph-spike.env \\
        --start 2026-09-01 --end 2026-10-31 --out ~/skillforge-spike [--probe-mailbox <other address>] \\
        [--subject <prefix> ...] [--category <label> ...]

Names, ids and categories off the allowlist map to keyed pseudonyms that stay stable across recordings (the
salt file). Dates move by a random whole number of weeks, drawn afresh for every recording and never stored
or printed, so neither one fixture nor several of them reveal the real dates.

The recording stays on the maintainer's machine: agents build synthetic fixtures from its shape and the summary,
and the maintainer deletes it after comparing them once (SP-CAL in ``docs/specs/billing.md``).

The inline metadata above gives ``uv run`` its own environment, so the project gains no runtime dependency.
The project environment has ``httpx`` but not ``msal``: the pure logic imports without it and the token
request imports it lazily, which keeps the module importable by the tests, ``ruff`` and ``ty``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import groupby
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

# region Constants

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
AUTHORITY_BASE_URL = "https://login.microsoftonline.com"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

LOCAL_TZ = ZoneInfo("Europe/Berlin")
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SALT_FILE = Path("~/.config/skillforge/graph-pseudonym.salt")
FIXTURE_NAME = "calendar-fixture.json"
FIXTURE_FORMAT = 1

CONFIG_KEYS = (
    "GRAPH__TENANT_ID",
    "GRAPH__CLIENT_ID",
    "GRAPH__CERTIFICATE_PATH",
    "GRAPH__PRIVATE_KEY_PATH",
    "GRAPH__MAILBOX",
    "GRAPH__CALENDAR_NAME",
)

# What billing needs from an event, nothing more: never body, attendees, organizer, webLink or onlineMeeting.
EVENT_FIELDS = (
    "id",
    "iCalUId",
    "seriesMasterId",
    "type",
    "subject",
    "start",
    "end",
    "originalStart",
    "isAllDay",
    "isCancelled",
    "isOnlineMeeting",
    "onlineMeetingProvider",
    "location",
    "categories",
    "showAs",
    "lastModifiedDateTime",
)
# The fixture keeps the order of lastModifiedDateTime as a rank: the instant lies near the recording date.
FIXTURE_EVENT_FIELDS = (*(field for field in EVENT_FIELDS if field != "lastModifiedDateTime"), "lastModifiedRank")
ID_FIELDS = ("id", "iCalUId", "seriesMasterId")
BOOLEAN_FIELDS = ("isAllDay", "isCancelled", "isOnlineMeeting")
# Graph's enums; any other value could carry text and becomes "unknown", as an unknown location type "default".
ENUM_FIELDS = {
    "type": frozenset({"singleInstance", "occurrence", "exception", "seriesMaster"}),
    "showAs": frozenset({"free", "tentative", "busy", "oof", "workingElsewhere", "unknown"}),
    "onlineMeetingProvider": frozenset({"unknown", "teamsForBusiness", "skypeForBusiness", "skypeForConsumer"}),
}
UNKNOWN_ENUM = "unknown"
LOCATION_TYPES = frozenset({
    "default",
    "conferenceRoom",
    "homeAddress",
    "businessAddress",
    "geoCoordinates",
    "streetAddress",
    "hotel",
    "restaurant",
    "localBusiness",
    "postalAddress",
})

PAGE_SIZE = 100
HTTP_TIMEOUT_SECONDS = 30
MAX_RETRIES = 4
DEFAULT_RETRY_AFTER_SECONDS = 5.0
MAX_RETRY_AFTER_SECONDS = 120.0

KENNENLERNEN = "Kennenlernen"
SUBJECTS = (
    "Mathe",
    "Mathematik",
    "Deutsch",
    "Englisch",
    "Französisch",
    "Latein",
    "Spanisch",
    "Italienisch",
    "Russisch",
    "Griechisch",
    "Physik",
    "Chemie",
    "Biologie",
    "Bio",
    "Informatik",
    "Geschichte",
    "Erdkunde",
    "Geographie",
    "Geografie",
    "Politik",
    "Sozialkunde",
    "Gemeinschaftskunde",
    "Wirtschaft",
    "Religion",
    "Ethik",
    "Philosophie",
    "Kunst",
    "Musik",
    "Sachunterricht",
    "Rechnungswesen",
    "BWL",
    "VWL",
    "Statistik",
)
# Outlook's preset categories (English and German UI): colour labels that name nobody. Any other category is
# pseudonymized unless --category names it.
DEFAULT_CATEGORIES = (
    "Red category",
    "Orange category",
    "Yellow category",
    "Green category",
    "Blue category",
    "Purple category",
    "Rote Kategorie",
    "Orange Kategorie",
    "Gelbe Kategorie",
    "Grüne Kategorie",
    "Blaue Kategorie",
    "Lila Kategorie",
)
# Words Outlook or the maintainer put in front of a subject; they name no person.
TITLE_MARKERS = ("Abgesagt", "Absage", "Canceled", "Cancelled")
PREFIX_SEPARATORS = re.compile(r"[\s&+/,:\-]*")

SUBJECT_PLACEHOLDER = "[subject]"
CATEGORY_PLACEHOLDER = "[category]"
UNMATCHED_PLACEHOLDER = "[unmatched]"
SYNTHETIC_LOCATIONS = {
    "empty": "",
    "phone": "+49 30 23125 000",  # a range the Bundesnetzagentur reserves for fiction
    "teams": "Microsoft Teams Meeting",
    "url": "https://example.org/meeting",
    "other": "Example location",
}

_NAME_SEPARATOR = re.compile(r"\s+mit\s+", re.IGNORECASE)
_PHONE_CHARS = re.compile(r"^[+\d\s/().\-]+$")
_MIN_PHONE_DIGITS = 6
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_CONSONANTS = "bdfgklmnprstvz"
_VOWELS = "aeiou"
_PSEUDONYM_SYLLABLES = 5
MIN_OFFSET_WEEKS = 52
MAX_OFFSET_WEEKS = 520
_PEM_CERTIFICATE = re.compile(r"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.DOTALL)

# endregion


# region Errors and configuration


class RecorderError(Exception):
    """A failure the script reports in one line; the message never carries calendar data or secrets."""


@dataclass(frozen=True, slots=True)
class GraphConfig:
    tenant_id: str
    client_id: str
    certificate_path: Path
    private_key_path: Path
    mailbox: str
    calendar_name: str


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _warn_if_shared(path: Path, what: str) -> None:
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        _warn(f"{what} is readable by others; run chmod 600 on it")


def parse_env_file(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines; blank lines, comments, ``export`` and surrounding quotes are allowed."""
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        if not sep:
            raise RecorderError(f"env file line {number} is not KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_config(env_file: Path) -> GraphConfig:
    """Read the config without ever echoing a value: errors name keys only."""
    env_file = env_file.expanduser()
    if not env_file.is_file():
        raise RecorderError("env file not found")
    _warn_if_shared(env_file, "the env file")
    values = parse_env_file(env_file.read_text())

    missing = [key for key in CONFIG_KEYS if not values.get(key)]
    if missing:
        raise RecorderError(f"env file lacks {', '.join(missing)}")
    if unknown := len(set(values) - set(CONFIG_KEYS)):
        # Only the count: a stray line may be a pasted secret, and its "key" is then the secret itself.
        _warn(f"env file has {unknown} unused key(s); they are ignored")

    config = GraphConfig(
        tenant_id=values["GRAPH__TENANT_ID"],
        client_id=values["GRAPH__CLIENT_ID"],
        certificate_path=Path(values["GRAPH__CERTIFICATE_PATH"]).expanduser(),
        private_key_path=Path(values["GRAPH__PRIVATE_KEY_PATH"]).expanduser(),
        mailbox=values["GRAPH__MAILBOX"],
        calendar_name=values["GRAPH__CALENDAR_NAME"],
    )
    for key, path in (
        ("GRAPH__CERTIFICATE_PATH", config.certificate_path),
        ("GRAPH__PRIVATE_KEY_PATH", config.private_key_path),
    ):
        if not path.is_file():
            raise RecorderError(f"{key} names no file")
    _warn_if_shared(config.private_key_path, "the private key")
    if "@" not in config.mailbox:
        raise RecorderError("GRAPH__MAILBOX is no mail address")
    return config


# endregion


# region Authentication


def certificate_pem(text: str) -> str:
    """The first certificate block of a PEM file, checked to be base64; a key in the same file stays behind."""
    match = _PEM_CERTIFICATE.search(text)
    try:
        if match is None:
            raise ValueError
        base64.b64decode("".join(match.group(1).split()), validate=True)
    except ValueError:
        raise RecorderError("GRAPH__CERTIFICATE_PATH holds no PEM certificate") from None
    return match.group()


def certificate_thumbprint(pem: str) -> str:
    """The SHA-1 thumbprint the Entra portal lists for the certificate, to compare when the token is refused."""
    der = base64.b64decode("".join(certificate_pem(pem).splitlines()[1:-1]))
    return hashlib.sha1(der, usedforsecurity=False).hexdigest().upper()


def acquire_token(config: GraphConfig) -> str:
    """Client credentials with a certificate.

    MSAL derives the SHA-256 thumbprint from ``public_certificate`` and signs a PS256 assertion (``x5t#S256``);
    passing a SHA-1 ``thumbprint`` instead is MSAL's deprecated RS256 path.
    """
    # A PEP 723 dependency that the project environment lacks on purpose; imported here so the rest loads without it.
    import msal  # ty: ignore[unresolved-import]

    certificate = config.certificate_path.read_text()
    public_certificate = certificate_pem(certificate)
    try:
        app = msal.ConfidentialClientApplication(
            config.client_id,
            authority=f"{AUTHORITY_BASE_URL}/{config.tenant_id}",
            client_credential={
                "private_key": config.private_key_path.read_text(),
                "public_certificate": public_certificate,
            },
        )
        result = app.acquire_token_for_client(scopes=[GRAPH_SCOPE])
    except ValueError as error:
        raise RecorderError(
            f"MSAL refused the key or certificate ({type(error).__name__}); is the key an unencrypted PEM?"
        ) from None
    except Exception as error:
        # A network error's text names the authority URL, and with it the tenant id.
        raise RecorderError(f"token request failed: {type(error).__name__}") from None
    if "access_token" not in result:
        # error_description carries tenant and trace ids; the code and the AADSTS numbers suffice to debug.
        codes = " ".join(str(code) for code in result.get("error_codes") or [])
        raise RecorderError(
            f"token request failed: {result.get('error')} {codes}".rstrip()
            + f"; the app registration must list the certificate {certificate_thumbprint(certificate)}"
        )
    return result["access_token"]


# endregion


# region Microsoft Graph


class GraphError(RecorderError):
    def __init__(self, status: int, code: str | None) -> None:
        super().__init__(f"Microsoft Graph answered {status}" + (f" ({code})" if code else ""))
        self.status = status


def _error_code(response: httpx.Response) -> str | None:
    """Graph's error code only (e.g. ``ErrorAccessDenied``); its message may name the mailbox."""
    try:
        error = response.json().get("error")
    except ValueError, AttributeError:
        return None
    return error.get("code") if isinstance(error, dict) else None


def _retry_after(response: httpx.Response) -> float:
    try:
        seconds = float(response.headers.get("Retry-After", DEFAULT_RETRY_AFTER_SECONDS))
    except ValueError:
        seconds = DEFAULT_RETRY_AFTER_SECONDS
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


class GraphClient:
    """GET-only access to Microsoft Graph: paging through ``@odata.nextLink``, bounded 429 retries."""

    def __init__(
        self,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Prefer": 'outlook.timezone="UTC"',
            },
            timeout=HTTP_TIMEOUT_SECONDS,
            transport=transport,
        )
        self._sleep = sleep

    def __enter__(self) -> GraphClient:
        return self

    def __exit__(self, *_: object) -> None:
        self._http.close()

    def get_json(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        if not url.startswith(f"{GRAPH_BASE_URL}/"):
            raise RecorderError("refusing to send the token outside Microsoft Graph")
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self._http.get(url, params=params)
            except httpx.HTTPError as error:
                # The exception text would carry the URL, and with it the mailbox.
                raise RecorderError(f"request to Microsoft Graph failed: {type(error).__name__}") from None
            if response.status_code == 429 and attempt < MAX_RETRIES:
                delay = _retry_after(response)
                _warn(f"throttled by Microsoft Graph; retry {attempt + 1}/{MAX_RETRIES} in {delay:g}s")
                self._sleep(delay)
                continue
            if response.is_success:
                body = response.json()
                if not isinstance(body, dict):
                    raise RecorderError("Microsoft Graph returned no JSON object")
                return body
            raise GraphError(response.status_code, _error_code(response))
        raise AssertionError("unreachable")

    def iter_values(self, url: str, params: dict[str, str] | None = None) -> Iterator[dict[str, Any]]:
        next_url: str | None = url
        while next_url:
            page = self.get_json(next_url, params)
            values = page.get("value")
            if not isinstance(values, list):
                raise RecorderError('Microsoft Graph returned a page without a "value" list')
            yield from values
            next_url = page.get("@odata.nextLink")
            params = None  # the next link carries every query parameter


def _user_url(mailbox: str) -> str:
    return f"{GRAPH_BASE_URL}/users/{quote(mailbox, safe='@')}"


def find_calendar_id(client: GraphClient, mailbox: str, name: str) -> str:
    calendars = list(client.iter_values(f"{_user_url(mailbox)}/calendars", {"$select": "id,name"}))
    for matches in (
        [c for c in calendars if c.get("name") == name],
        [c for c in calendars if str(c.get("name", "")).casefold() == name.casefold()],
    ):
        if len(matches) == 1:
            return matches[0]["id"]
        if len(matches) > 1:
            raise RecorderError("GRAPH__CALENDAR_NAME matches several calendars")
    raise RecorderError(f"GRAPH__CALENDAR_NAME matches none of the {len(calendars)} visible calendars")


def local_window(start: date, end: date) -> tuple[datetime, datetime]:
    """``[start 00:00, end + 1 day 00:00)`` in Europe/Berlin, as UTC: ``--end`` is inclusive."""
    if end < start:
        raise RecorderError("--end lies before --start")
    first = datetime.combine(start, datetime.min.time(), LOCAL_TZ)
    after_last = datetime.combine(end + timedelta(days=1), datetime.min.time(), LOCAL_TZ)
    return first.astimezone(UTC), after_last.astimezone(UTC)


def fetch_events(client: GraphClient, mailbox: str, calendar_id: str, start: date, end: date) -> list[dict[str, Any]]:
    window_start, window_end = local_window(start, end)
    url = f"{_user_url(mailbox)}/calendars/{quote(calendar_id, safe='')}/calendarView"
    params = {
        "startDateTime": window_start.isoformat().replace("+00:00", "Z"),
        "endDateTime": window_end.isoformat().replace("+00:00", "Z"),
        "$select": ",".join(EVENT_FIELDS),
        "$top": str(PAGE_SIZE),
    }
    return list(client.iter_values(url, params))


def probe_mailbox(client: GraphClient, mailbox: str) -> str:
    """One GET against a mailbox outside the RBAC scope: 403 proves the restriction."""
    try:
        client.get_json(f"{_user_url(mailbox)}/calendars", {"$select": "id", "$top": "1"})
    except GraphError as error:
        return "yes" if error.status == 403 else f"inconclusive (HTTP {error.status})"
    return "no"


# endregion


# region Pseudonymization


def normalize_token(token: str) -> str:
    """NFKC, casefold and transliterated umlauts, as the name matching normalises; typos stay distinct."""
    return unicodedata.normalize("NFKC", token).casefold().translate(_UMLAUTS)


def is_phone_number(text: str) -> bool:
    text = text.strip()
    return bool(text) and bool(_PHONE_CHARS.match(text)) and sum(ch.isdigit() for ch in text) >= _MIN_PHONE_DIGITS


def classify_location(location: Any) -> str:
    """One of ``empty``, ``phone``, ``teams``, ``url`` or ``other``."""
    location = location if isinstance(location, dict) else {}
    name = str(location.get("displayName") or "").strip()
    uri = str(location.get("locationUri") or "").strip()
    if not name and not uri:
        return "empty"
    if is_phone_number(name):
        return "phone"
    if "teams" in name.casefold() or "teams.microsoft" in uri.casefold():
        return "teams"
    if "://" in name or "://" in uri:
        return "url"
    return "other"


def _shift_local(moment: datetime, weeks: int) -> datetime:
    """Shift an aware instant by whole weeks of Europe/Berlin wall-clock time, across DST changes."""
    local = moment.astimezone(LOCAL_TZ).replace(tzinfo=None) + timedelta(weeks=weeks)
    return local.replace(tzinfo=LOCAL_TZ).astimezone(UTC)


def _parse_utc(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _graph_datetime(moment: datetime) -> str:
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond:06d}0"


def random_week_offset() -> int:
    """A fresh shift of 52 to 520 weeks either way; never derived from the salt, so recordings do not share it."""
    weeks = MIN_OFFSET_WEEKS + secrets.randbelow(MAX_OFFSET_WEEKS - MIN_OFFSET_WEEKS + 1)
    return weeks if secrets.randbelow(2) else -weeks


def last_modified_ranks(raw_events: Sequence[dict[str, Any]]) -> list[int | None]:
    """Dense rank of ``lastModifiedDateTime`` (0 = earliest): the order survives, the instants do not."""
    instants: list[datetime | None] = []
    for raw in raw_events:
        try:
            instants.append(_parse_utc(raw["lastModifiedDateTime"]))
        except KeyError, TypeError, ValueError:
            instants.append(None)
    rank = {moment: index for index, moment in enumerate(sorted({m for m in instants if m is not None}))}
    return [None if moment is None else rank[moment] for moment in instants]


def _is_word_char(char: str) -> bool:
    return unicodedata.category(char)[0] in "LMN"


def _runs(text: str) -> list[tuple[bool, str]]:
    """Maximal runs of letters, marks and digits (``True``) and of everything else (``False``).

    By Unicode category, so a combining mark that NFC cannot compose (a virama, say) stays inside its word.
    """
    return [(is_word, "".join(run)) for is_word, run in groupby(text, key=_is_word_char)]


def _words(text: str) -> list[str]:
    return [run for is_word, run in _runs(text) if is_word]


def _category_key(category: str) -> str:
    return " ".join(normalize_token(category).split())


class Pseudonymizer:
    """Keyed replacements: the same salt maps a name or id the same way across runs; the date shift is per run."""

    def __init__(self, salt: bytes, extra_subjects: Iterable[str] = (), extra_categories: Iterable[str] = ()) -> None:
        self._name_key = self._derive(salt, b"names")
        self._id_key = self._derive(salt, b"ids")
        self._week_offset = random_week_offset()
        kept = (*SUBJECTS, *TITLE_MARKERS, KENNENLERNEN, *extra_subjects)
        self._kept_words = {normalize_token(word) for text in kept for word in _words(text)}
        self._kept_categories = {_category_key(category) for category in (*DEFAULT_CATEGORIES, *extra_categories)}
        self._pseudonyms: dict[str, str] = {}

    @staticmethod
    def _derive(salt: bytes, label: bytes) -> bytes:
        return hmac.new(salt, label, hashlib.sha256).digest()

    # Ids

    def pseudo_id(self, value: Any) -> str | None:
        if value is None:
            return None
        return hmac.new(self._id_key, str(value).encode(), hashlib.sha256).hexdigest()[:32]

    # Names and titles

    def _word(self, word: str) -> str:
        """Digits stay digits and an initial stays one letter; any other word becomes a five-syllable name."""
        normalized = normalize_token(word)
        digest = hmac.new(self._name_key, normalized.encode(), hashlib.sha256).digest()
        if word.isdigit():
            return "".join(str(byte % 10) for byte in digest[: len(word)])
        if len(word) == 1:
            letter = _CONSONANTS[digest[0] % len(_CONSONANTS)]
            return letter if word.islower() else letter.upper()

        pseudonym = "".join(
            _CONSONANTS[digest[2 * i] % len(_CONSONANTS)] + _VOWELS[digest[2 * i + 1] % len(_VOWELS)]
            for i in range(_PSEUDONYM_SYLLABLES)
        )
        # Two people sharing a pseudonym would silently merge them in the fixture; 70**5 makes it unlikely.
        if self._pseudonyms.setdefault(pseudonym, normalized) != normalized:
            raise RecorderError("two name tokens collided on one pseudonym; move the salt file away and rerun")
        if word.isupper():
            return pseudonym.upper()
        return pseudonym if word[0].islower() else pseudonym.capitalize()

    def pseudo_name(self, name: str) -> str:
        """Replace every word, keep spaces, hyphens and punctuation: token count and shape survive.

        NFC first, so a decomposed umlaut is one letter; a mark NFC leaves alone stays inside its word anyway.
        """
        runs = _runs(unicodedata.normalize("NFC", name))
        return "".join(self._word(run) if is_word else run for is_word, run in runs)

    def keeps_prefix(self, prefix: str) -> bool:
        runs = _runs(prefix)
        words = [run for is_word, run in runs if is_word]
        return (
            bool(words)
            and all(normalize_token(w) in self._kept_words for w in words)
            and all(PREFIX_SEPARATORS.fullmatch(gap) for is_word, gap in runs if not is_word)
        )

    def pseudo_subject(self, subject: Any) -> str:
        """``<prefix> mit <name>`` keeps an allowlisted prefix and pseudonymizes the name; else a placeholder."""
        subject = unicodedata.normalize("NFC", str(subject or "")).strip()
        separator = _NAME_SEPARATOR.search(subject)
        if separator is None or not subject[separator.end() :].strip():
            return UNMATCHED_PLACEHOLDER
        prefix = subject[: separator.start()]
        kept = prefix if self.keeps_prefix(prefix) else SUBJECT_PLACEHOLDER
        return f"{kept}{separator.group()}{self.pseudo_name(subject[separator.end() :])}"

    def pseudo_category(self, category: Any) -> str | None:
        """An allowlisted category stays; any other may name a person and becomes ``[category] <pseudonym>``.

        A blank category is ``None``: the caller drops it.
        """
        text = unicodedata.normalize("NFC", str(category)).strip()
        if not text:
            return None
        if _category_key(text) in self._kept_categories:
            return text
        return f"{CATEGORY_PLACEHOLDER} {self.pseudo_name(text)}"

    # Dates

    def shift_datetime_tz(self, value: Any, *, all_day: bool) -> Any:
        """A Graph ``dateTimeTimeZone`` (UTC by the ``Prefer`` header); all-day dates are floating midnights."""
        if not isinstance(value, dict) or not value.get("dateTime"):
            return value
        moment = datetime.fromisoformat(value["dateTime"]).replace(tzinfo=None)
        if all_day:
            shifted = moment + timedelta(weeks=self._week_offset)
        else:
            shifted = _shift_local(moment.replace(tzinfo=UTC), self._week_offset)
        return {"dateTime": _graph_datetime(shifted), "timeZone": value.get("timeZone", "UTC")}

    def shift_instant(self, value: Any, *, all_day: bool) -> Any:
        """An ISO instant; for an all-day event in plain whole weeks like its dates, else on the Berlin clock.

        On the Berlin clock an all-day ``originalStart`` would move by an hour across DST and give the season away.
        """
        if not isinstance(value, str) or not value:
            return value
        moment = _parse_utc(value)
        if all_day:
            shifted = moment + timedelta(weeks=self._week_offset)
        else:
            shifted = _shift_local(moment, self._week_offset)
        return f"{shifted:%Y-%m-%dT%H:%M:%S}Z"

    # Events

    def events(self, raw_events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        ranks = last_modified_ranks(raw_events)
        return [self.event(raw, last_modified_rank=rank) for raw, rank in zip(raw_events, ranks, strict=True)]

    def event(self, raw: dict[str, Any], *, last_modified_rank: int | None = None) -> dict[str, Any]:
        """A new event built from an allowlist of fields; anything unknown is dropped, nothing is copied blindly."""
        categories = raw.get("categories")
        all_day = bool(raw.get("isAllDay"))
        location = raw.get("location")
        location = location if isinstance(location, dict) else {}
        location_type = location.get("locationType")
        out: dict[str, Any] = {field: self.pseudo_id(raw.get(field)) for field in ID_FIELDS}
        out |= {field: raw[field] if isinstance(raw.get(field), bool) else None for field in BOOLEAN_FIELDS}
        out |= {
            field: None if raw.get(field) is None else raw[field] if raw[field] in allowed else UNKNOWN_ENUM
            for field, allowed in ENUM_FIELDS.items()
        }
        out |= {
            "subject": self.pseudo_subject(raw.get("subject")),
            "start": self.shift_datetime_tz(raw.get("start"), all_day=all_day),
            "end": self.shift_datetime_tz(raw.get("end"), all_day=all_day),
            "originalStart": self.shift_instant(raw.get("originalStart"), all_day=all_day),
            "location": {
                "displayName": SYNTHETIC_LOCATIONS[classify_location(location)],
                "locationType": location_type if location_type in LOCATION_TYPES else "default",
            },
            "categories": [
                pseudonym
                for c in (categories if isinstance(categories, list) else [])
                if (pseudonym := self.pseudo_category(c))
            ],
            "lastModifiedRank": last_modified_rank,
        }
        return {field: out[field] for field in FIXTURE_EVENT_FIELDS}


def load_or_create_salt(path: Path) -> bytes:
    """32 random bytes, created once with mode 600; losing the file only changes the pseudonyms."""
    path = path.expanduser()
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(secrets.token_hex(32) + "\n")
    _warn_if_shared(path, "the salt file")
    try:
        salt = bytes.fromhex(path.read_text().strip())
    except ValueError:
        raise RecorderError("the salt file holds no hex salt") from None
    if len(salt) < 16:
        raise RecorderError("the salt file holds less than 16 bytes")
    return salt


# endregion


# region Output


def subject_prefix(subject: str) -> str | None:
    """The text before `` mit ``, or ``None`` for an unmatched title."""
    parts = _NAME_SEPARATOR.split(subject, maxsplit=1)
    return parts[0].strip() if len(parts) == 2 else None


def classify_title(subject: str) -> str:
    """``lesson``, ``kennenlernen`` or ``unmatched``, read from a pseudonymized subject."""
    prefix = subject_prefix(subject)
    if prefix is None:
        return "unmatched"
    return "kennenlernen" if normalize_token(prefix) == normalize_token(KENNENLERNEN) else "lesson"


def _duration_minutes(event: dict[str, Any]) -> int | None:
    try:
        start = _parse_utc(event["start"]["dateTime"])
        end = _parse_utc(event["end"]["dateTime"])
    except KeyError, TypeError, ValueError:
        return None
    return round((end - start).total_seconds() / 60)


def _section(title: str, counts: Counter[Any], *, by_key: bool = False) -> list[str]:
    if not counts:
        return [f"{title}: none"]
    items = sorted(counts.items()) if by_key else sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))
    return [f"{title}:", *(f"  {key}: {count}" for key, count in items)]


def summarize(events: Sequence[dict[str, Any]], *, window_days: int, probe: str | None) -> str:
    """Counts only - no names, ids, addresses, tenant data or dates. Built from pseudonymized events."""
    titles = Counter(classify_title(e["subject"]) for e in events)
    prefixes = Counter(prefix for e in events if (prefix := subject_prefix(e["subject"])) is not None)
    categories = Counter(c for e in events for c in e["categories"]) + Counter(
        "(none)" for e in events if not e["categories"]
    )
    online = Counter(str(e.get("onlineMeetingProvider")) for e in events if e.get("isOnlineMeeting"))
    durations = Counter(
        minutes for e in events if not e.get("isAllDay") and (minutes := _duration_minutes(e)) is not None
    )
    lines = [
        "SkillForge calendar spike - summary (pseudonymized; read it before you share it)",
        f"window: {window_days} days",
        f"events: {len(events)}",
        f"  lessons (<subject> mit <name>): {titles['lesson']}",
        f"  kennenlernen: {titles['kennenlernen']}",
        f"  unmatched titles: {titles['unmatched']}",
        f"cancelled (isCancelled): {sum(1 for e in events if e.get('isCancelled'))}",
        f"all-day: {sum(1 for e in events if e.get('isAllDay'))}",
        f"online meetings: {sum(online.values())}",
        *[f"  {provider}: {count}" for provider, count in sorted(online.items())],
        *_section("recurrence type", Counter(str(e.get("type")) for e in events)),
        *_section("show as", Counter(str(e.get("showAs")) for e in events)),
        *_section("location classes", Counter(classify_location(e["location"]) for e in events)),
        *_section(f"categories ({CATEGORY_PLACEHOLDER} = not on the allowlist, pseudonymized)", categories),
        *_section(f"subject prefixes ({SUBJECT_PLACEHOLDER} = not on the allowlist)", prefixes),
        *_section("durations in minutes (all-day excluded)", durations, by_key=True),
        f"probe of another mailbox: {f'restricted: {probe}' if probe else 'not run'}",
    ]
    return "\n".join(lines)


def _in_git_work_tree(path: Path) -> bool:
    return path.is_relative_to(REPO_ROOT) or any((folder / ".git").exists() for folder in (path, *path.parents))


def check_out_dir(out: Path, *, force: bool) -> Path:
    """Refuse any git work tree (the recording never enters a repository), a symlink and an unforced overwrite."""
    out = out.expanduser().resolve()
    if _in_git_work_tree(out):
        raise RecorderError("--out lies inside a git work tree; write outside every repository and review it first")
    fixture = out / FIXTURE_NAME
    if fixture.is_symlink():
        raise RecorderError(f"{FIXTURE_NAME} in --out is a symbolic link; remove it first")
    if fixture.exists() and not force:
        raise RecorderError(f"{FIXTURE_NAME} exists in --out; pass --force to overwrite it")
    return fixture


def write_fixture(path: Path, document: dict[str, Any]) -> None:
    """Write a fresh mode-600 file next to the target and rename it over: an old file's mode never survives."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise RecorderError(f"{FIXTURE_NAME} in --out is a symbolic link; remove it first")
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{FIXTURE_NAME}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


# endregion


# region CLI


class _QuietParser(argparse.ArgumentParser):
    """Its errors echo nothing the maintainer typed: a mistyped option or value may be an address or a real date."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: the arguments do not parse (an option or a value); see --help\n")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _QuietParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--env-file", type=Path, required=True, help="the Graph config (mode 600)")
    parser.add_argument("--start", type=date.fromisoformat, required=True, help="first day, YYYY-MM-DD (Berlin)")
    parser.add_argument("--end", type=date.fromisoformat, required=True, help="last day, inclusive, YYYY-MM-DD")
    parser.add_argument("--out", type=Path, required=True, help="directory for the fixture (outside every git repo)")
    parser.add_argument("--force", action="store_true", help="overwrite an existing fixture")
    parser.add_argument("--probe-mailbox", help="another mailbox; expects 403 to prove the RBAC restriction")
    parser.add_argument("--subject", action="append", default=[], help="keep this subject prefix too (repeatable)")
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="keep this category verbatim too (repeatable); it must name nobody",
    )
    parser.add_argument("--salt-file", type=Path, default=DEFAULT_SALT_FILE, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    token_provider: Callable[[GraphConfig], str] = acquire_token,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = _parse_args(argv)
    try:
        fixture_path = check_out_dir(args.out, force=args.force)
        local_window(args.start, args.end)  # rejects a reversed window before any request
        config = load_config(args.env_file)
        if args.probe_mailbox and args.probe_mailbox.casefold() == config.mailbox.casefold():
            raise RecorderError("--probe-mailbox must name another mailbox")
        pseudonymizer = Pseudonymizer(load_or_create_salt(args.salt_file), args.subject, args.category)

        with GraphClient(token_provider(config), transport=transport, sleep=sleep) as client:
            calendar_id = find_calendar_id(client, config.mailbox, config.calendar_name)
            raw_events = fetch_events(client, config.mailbox, calendar_id, args.start, args.end)
            probe = probe_mailbox(client, args.probe_mailbox) if args.probe_mailbox else None

        events = pseudonymizer.events(raw_events)
        del raw_events, pseudonymizer  # from here on only pseudonymized data exists, and no date offset
        window_days = (args.end - args.start).days + 1

        write_fixture(
            fixture_path,
            {
                "meta": {
                    "format": FIXTURE_FORMAT,
                    "recorder": "scripts/graph/record_calendar.py",
                    "pseudonymized": True,
                    "timeZone": "UTC",
                    # The length only: the window's dates would give the shift away.
                    "windowDays": window_days,
                    "dateShift": "a random whole number of weeks per recording, Europe/Berlin wall clock kept",
                    "fields": list(FIXTURE_EVENT_FIELDS),
                },
                "value": events,
            },
        )
        print(summarize(events, window_days=window_days, probe=probe))
    except RecorderError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        # The type only: the text of an unexpected error may carry a real date, an address or a tenant id.
        print(f"error: unexpected {type(error).__name__}; its message is withheld", file=sys.stderr)
        return 1

    # No path here: it names the home directory, and this output may be pasted along with the summary.
    print(f"\n{FIXTURE_NAME} written to --out; review it before it goes anywhere", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# endregion
