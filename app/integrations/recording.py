"""The recorder: writes request/response pairs as JSON fixtures, scrubbed of anything that identifies someone.

It is an ``Observer`` of ``IntegrationHttp`` and records nothing until it is armed - after the identity check passed.
The scrub works in three layers:

- objects of people and companies (``SevClient``, ``SevUser``, ``Contact``, ``ContactAddress``,
  ``CommunicationWay``) keep only ids, ``objectName``, timestamps and their references - every other string goes;
- in every other object a string under a key that may hold a person's data or free text (a name, an address, bank
  or contact details, a text or a note) goes, unless it carries the run id of the spike that wrote it -
  configuration objects (units, parts, custom field settings, sequences) keep their names;
- in every string left, keys and the request path included: e-mail addresses (to ``example.org``), IBANs, phone
  numbers, hex tokens, the trial marker, base64 blobs and every value scrubbed before.

``tests/test_fixture_scan.py`` checks the result with patterns of its own. Recorded fixtures are never edited.
"""

import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .transport import Exchange

SCRUBBED = "[SCRUBBED]"
MAX_STRING = 2000
"""Longer strings - a PDF as base64 - are recorded by size only."""
TRIAL_MARKER = "TRIAL"

_RECORDED_HEADERS = frozenset({"content-type", "accept", "user-agent"})
"""Only headers that describe the format are kept: credentials and cookies never reach a fixture."""
_PEOPLE = frozenset({"SevClient", "SevUser", "Contact", "ContactAddress", "CommunicationWay"})
"""Objects of people and companies: everything but the structure goes."""
_STRUCTURE = frozenset({"id", "objectName"})
_TIMESTAMPS = frozenset({"create", "update"})
_CONFIGURATION = frozenset({"Unity", "Part", "ContactCustomFieldSetting", "SevSequence", "TaxRule", "Category"})
"""Objects of the account's configuration: their names are no one's data."""
_SENSITIVE_KEY_PARTS = (
    "address",
    "name",
    "street",
    "zip",
    "city",
    "country",
    "iban",
    "bic",
    "bank",
    "phone",
    "mobile",
    "fax",
    "mail",
    "web",
    "birth",
    "vat",
    "taxnumber",
    "owner",
    "signature",
    "salutation",
    "title",
    "description",
    "text",
    "header",
    "note",
    "value",
)
ACCOUNT_ID = "0"
"""The id of the account every object belongs to, wherever it is named; recorded as a placeholder."""

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?")
_EMAIL = re.compile(r"[\w.%+-]+(?:@|%40|\(at\))(?P<domain>[\w-]+(?:\.[\w-]+)*\.[a-z]{2,})", re.IGNORECASE)
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[   ]?[A-Z0-9]{4}){2,7}(?:[   ]?[A-Z0-9]{1,3})?\b", re.I)
_PHONE = re.compile(r"(?<![\w+])(?:\+|\(?0)[\d   ()./-]*\d")
_PHONE_DIGITS = 9
"""A phone number has at least this many digits; a date (``01.10.2026``) has fewer."""
_HEX_TOKEN = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32,}(?![0-9a-fA-F])")
_BASE64 = re.compile(r"[A-Za-z0-9+/=]{64,}")


def _normalized(key: str) -> str:
    return key.replace("-", "").replace("_", "").lower()


def _learnable(value: object) -> bool:
    """A value worth replacing wherever it shows up again: words, not numbers such as a status or a postal code."""
    return isinstance(value, str) and len(value) >= 3 and any(char.isalpha() for char in value)


def _phone(match: re.Match[str]) -> str:
    return "[PHONE]" if sum(char.isdigit() for char in match[0]) >= _PHONE_DIGITS else match[0]


def _email(match: re.Match[str]) -> str:
    return match[0] if match["domain"].lower() == "example.org" and "@" in match[0] else "person@example.org"


class Scrubber:
    """Scrubs JSON-like data; every value it scrubbed by key is also replaced wherever it shows up later.

    ``keep`` is the run id of the spike whose objects are recorded: a free text carrying it is the spike's own.
    """

    def __init__(self, keep: str | None = None) -> None:
        self._keep = keep
        self._known: set[str] = set()

    def __call__(self, data: object) -> object:
        self.learn(data)
        return self._scrub(data)

    def text(self, value: str, *, blobs: bool = True) -> str:
        """``value`` with every pattern and every known value replaced; ``blobs=False`` for a URL path."""
        if len(value) > MAX_STRING:
            return f"[{len(value)} characters]"
        if blobs:
            value = _BASE64.sub(lambda match: f"[{len(match[0])} characters]", value)
        value = _HEX_TOKEN.sub("[TOKEN]", value)
        value = _EMAIL.sub(_email, value)
        value = _IBAN.sub("[IBAN]", value)
        value = _PHONE.sub(_phone, value)
        for known in sorted(self._known, key=len, reverse=True):
            value = value.replace(known, SCRUBBED)
        return value.replace(TRIAL_MARKER, "[MARKER]")

    def _sensitive(self, key: str, value: object, parent: dict[str, object]) -> bool:
        """Whether ``value``, a string under ``key`` of ``parent``, goes as a whole."""
        if not isinstance(value, str):
            return False
        object_name = parent.get("objectName")
        if object_name in _PEOPLE:
            return key not in _STRUCTURE and not (key in _TIMESTAMPS and _TIMESTAMP.fullmatch(value))
        if key == "objectName" or object_name in _CONFIGURATION:
            return False
        if self._own(value):
            return False
        normalized = _normalized(key)
        return any(part in normalized for part in _SENSITIVE_KEY_PARTS)

    def _own(self, value: str) -> bool:
        """Whether ``value`` is the spike's own: it carries the run id."""
        return self._keep is not None and self._keep in value

    def learn(self, data: object) -> None:
        """Remember every value ``data`` holds in a sensitive place, to replace it wherever it shows up."""
        match data:
            case dict():
                for key, value in data.items():
                    if _learnable(value) and not self._own(value) and self._sensitive(key, value, data):
                        self._known.add(value)
                    self.learn(value)
            case list():
                for item in data:
                    self.learn(item)

    def _scrub(self, data: object) -> object:
        match data:
            case dict():
                return {self.text(key): self._scrub_entry(key, value, data) for key, value in data.items()}
            case list():
                return [self._scrub(item) for item in data]
            case str():
                return self.text(data)
        return data

    def _scrub_entry(self, key: str, value: object, parent: dict[str, object]) -> object:
        if key == "id" and parent.get("objectName") == "SevClient":
            return ACCOUNT_ID
        if self._sensitive(key, value, parent):
            return SCRUBBED
        return self._scrub(value)


class Recorder:
    """An observer that writes each exchange to ``<directory>/<step>-<NN>.json``; set ``step`` before each step.

    It drops every exchange until ``arm`` is called, so nothing of an account the identity check refused is kept.
    """

    def __init__(self, directory: Path, *, keep: str | None = None) -> None:
        self.directory = directory
        self.step = "exchange"
        self._armed = False
        self._scrub = Scrubber(keep)
        self._count = 0

    def arm(self) -> None:
        self._armed = True

    def __call__(self, exchange: Exchange) -> None:
        if not self._armed:
            return
        self._count += 1
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{self.step}-{self._count:02d}.json"
        path.write_text(json.dumps(self.record(exchange), indent=2, ensure_ascii=False) + "\n")

    def record(self, exchange: Exchange) -> dict[str, object]:
        """The scrubbed record; what the exchange reveals anywhere is learned before any part of it is scrubbed."""
        url = urlsplit(exchange.url)
        params = dict(parse_qsl(url.query))
        try:
            response: object = json.loads(exchange.response_content)
        except ValueError:
            response = f"[{len(exchange.response_content)} bytes {exchange.response_headers.get('content-type', '')}]"
        for data in (params, exchange.request_body, response):
            self._scrub.learn(data)
        return {
            "request": {
                "method": exchange.method,
                "path": self._scrub.text(url.path, blobs=False),
                "params": self._scrub(params),
                "headers": _headers(exchange.request_headers),
                "body": self._scrub(exchange.request_body),
            },
            "response": {
                "status": exchange.status,
                "headers": _headers(exchange.response_headers),
                "body": self._scrub(response),
            },
        }


def _headers(headers: dict[str, str]) -> dict[str, str]:
    return {name: value for name, value in headers.items() if name.lower() in _RECORDED_HEADERS}
