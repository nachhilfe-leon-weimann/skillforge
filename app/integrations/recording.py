"""The recorder: writes request/response pairs as JSON fixtures, scrubbed of anything that identifies someone.

It is an ``Observer`` of ``IntegrationHttp`` and records nothing until it is armed - after the identity check passed.
Each run writes into a directory of its own, named by its run id, and never overwrites a file. The scrub works in
three layers:

- objects of people and companies (``SevClient``, ``SevUser``, ``Contact``, ``ContactAddress``,
  ``CommunicationWay``) keep only ids, ``objectName``, timestamps, references and the few fields
  ``_PEOPLE`` allows per type - at every depth, nested objects and lists included; every other value goes;
- in every other object a string under a key that may hold a person's data or free text (a name, an address, bank
  or contact details, a text or a note) goes, unless the run sent exactly that string itself or it is a payment
  method - configuration objects (units, parts, custom field settings, sequences) keep their names;
- in every string left, keys and the request path included: e-mail addresses (to ``example.org``), IBANs, phone
  numbers, hex tokens, the trial marker, base64 blobs and every value a response revealed in a scrubbed place.

``tests/test_fixture_scan.py`` checks the result with patterns of its own. Recorded fixtures are never edited.
"""

import json
import re
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .transport import Exchange

SCRUBBED = "[SCRUBBED]"
MAX_STRING = 2000
"""Longer strings - a PDF as base64 - are recorded by size only."""
TRIAL_MARKER = "TRIAL"
RUN_ID = re.compile(r"run\d{18}")
"""A run id: a fixed prefix and digits only (time and a random suffix), so no scrub pattern can match it."""
PAYMENT_METHODS = frozenset({"PayPal", "Überweisung"})
"""The values of the custom field "Zahlungsverfahren": no one's data, and what the billing reads."""

_RECORDED_HEADERS = frozenset({"content-type", "accept", "user-agent"})
"""Only headers that describe the format are kept: credentials and cookies never reach a fixture."""
_STRUCTURE = frozenset({"id", "objectName"})
_TIMESTAMPS = frozenset({"create", "update"})
_PEOPLE: dict[str, frozenset[str]] = {
    "SevClient": frozenset(),
    "SevUser": frozenset(),
    "Contact": frozenset({"customerNumber", "status"}),
    "ContactAddress": frozenset(),
    "CommunicationWay": frozenset({"type", "main"}),
}
"""Objects of people and companies, each with the fields it keeps besides structure, timestamps and references."""
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
_DATE = re.compile(r"\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2}")
"""A date as sevDesk writes it (``01.10.2026``, ``2026-10-01``): no part of a phone number."""
_EMAIL = re.compile(r"[\w.%+-]+(?:@|%40|\(at\))(?P<domain>[\w-]+(?:\.[\w-]+)*\.[a-z]{2,})", re.IGNORECASE)
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ \u00a0\u202f]?[A-Z0-9]{4}){2,7}(?:[ \u00a0\u202f]?[A-Z0-9]{1,3})?\b", re.I)
_PHONE = re.compile(r"(?<![\w+])(?:\+|\(?0)[\d \u00a0\u202f()./-]*\d")
_PHONE_DIGITS = 9
"""A phone number has at least this many digits besides any date in the same run of characters."""
_HEX_TOKEN = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32,}(?![0-9a-fA-F])")
_BASE64 = re.compile(r"[A-Za-z0-9+/=]{64,}")


def new_run_id(now: datetime) -> str:
    return f"run{now:%Y%m%d%H%M%S}{secrets.randbelow(10_000):04d}"


def _normalized(key: str) -> str:
    return key.replace("-", "").replace("_", "").lower()


def _learnable(value: object) -> bool:
    """A value worth replacing wherever it shows up again: words, not numbers such as a status or a postal code."""
    return isinstance(value, str) and len(value) >= 3 and any(char.isalpha() for char in value)


def _phone(match: re.Match[str]) -> str:
    digits = sum(char.isdigit() for char in _DATE.sub("", match[0]))
    return "[PHONE]" if digits >= _PHONE_DIGITS else match[0]


def _email(match: re.Match[str]) -> str:
    return match[0] if match["domain"].lower() == "example.org" and "@" in match[0] else "person@example.org"


def _strings(data: object) -> set[str]:
    match data:
        case dict():
            return {found for value in data.values() for found in _strings(value)}
        case list():
            return {found for item in data for found in _strings(item)}
        case str():
            return {data}
    return set()


class Scrubber:
    """Scrubs JSON-like data; every value a response revealed in a scrubbed place is replaced wherever it shows up.

    The strings the run sent itself (``sent``) are its own: outside people objects they stay as they are.
    """

    def __init__(self) -> None:
        self._own: set[str] = set()
        self._known: set[str] = set()

    def __call__(self, data: object) -> object:
        return self._scrub(data, people=None)

    def sent(self, data: object) -> None:
        """Remember every string of a request as the run's own; nothing is learned from it."""
        self._own |= _strings(data)

    def learn(self, data: object) -> None:
        """Remember every value a response holds in a scrubbed place, to replace it wherever it shows up."""
        self._scrub(data, people=None, learning=True)

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

    def _scrub(self, data: object, *, people: frozenset[str] | None, learning: bool = False) -> object:
        """``people`` is the allowlist of the people object ``data`` lies in, ``None`` outside of one."""
        match data:
            case dict():
                object_name = data.get("objectName")
                if isinstance(object_name, str):
                    people = _PEOPLE.get(object_name)
                elif people is not None:
                    people = frozenset()  # a nested object without a name of its own: structure only
                return {
                    self.text(key): self._entry(key, value, object_name, people, learning)
                    for key, value in data.items()
                }
            case list():
                return [self._scrub(item, people=people, learning=learning) for item in data]
            case str():
                return self.text(data) if people is None else self._gone(data, learning)
            case None:
                return None
        return data if people is None else self._gone(data, learning)

    def _entry(
        self, key: str, value: object, object_name: object, people: frozenset[str] | None, learning: bool
    ) -> object:
        if key == "id" and object_name == "SevClient":
            return ACCOUNT_ID
        if isinstance(value, dict | list):
            return self._scrub(value, people=people, learning=learning)
        if people is not None:
            if key in _STRUCTURE or key in people or (key in _TIMESTAMPS and _is_timestamp(value)):
                return self._scrub(value, people=None)
            return self._scrub(value, people=people, learning=learning)
        if isinstance(value, str) and self._sensitive(key, value, object_name):
            return self._gone(value, learning)
        return self._scrub(value, people=None)

    def _sensitive(self, key: str, value: str, object_name: object) -> bool:
        """Whether ``value``, a string under ``key`` of an object that is no person's, goes as a whole."""
        if key == "objectName" or object_name in _CONFIGURATION or value in self._own or value in PAYMENT_METHODS:
            return False
        return any(part in _normalized(key) for part in _SENSITIVE_KEY_PARTS)

    def _gone(self, value: object, learning: bool) -> str:
        if learning and _learnable(value) and value not in self._own and value not in PAYMENT_METHODS:
            self._known.add(str(value))
        return SCRUBBED


def _is_timestamp(value: object) -> bool:
    return isinstance(value, str) and _TIMESTAMP.fullmatch(value) is not None


class Recorder:
    """An observer that writes each exchange to ``<directory>/<run id>/<step>-<NN>.json``; set ``step`` before each
    step. A run id used again continues the numbering; an existing file is never overwritten.

    It drops every exchange until ``arm`` is called, so nothing of an account the identity check refused is kept.
    A failure to record is kept in ``failure`` - the transport only logs it - so the run can stop on it.
    """

    def __init__(self, directory: Path, *, run_id: str) -> None:
        if not RUN_ID.fullmatch(run_id):
            raise ValueError(f"a run id is 'run' and 18 digits, not {run_id!r}")
        self.directory = directory / run_id
        self.step = "exchange"
        self.failure: str | None = None
        self._armed = False
        self._scrub = Scrubber()
        self._count = len(list(self.directory.glob("*.json"))) if self.directory.is_dir() else 0

    def arm(self) -> None:
        self._armed = True

    def __call__(self, exchange: Exchange) -> None:
        if not self._armed:
            return
        try:
            record = json.dumps(self.record(exchange), indent=2, ensure_ascii=False) + "\n"
            self._count += 1
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / f"{self.step}-{self._count:02d}.json").open("x") as file:
                file.write(record)
        except Exception as exc:
            self.failure = self.failure or type(exc).__name__
            raise

    def record(self, exchange: Exchange) -> dict[str, object]:
        """The scrubbed record; what the response reveals is learned before any part of the exchange is scrubbed."""
        url = urlsplit(exchange.url)
        params = dict(parse_qsl(url.query))
        try:
            response: object = json.loads(exchange.response_content)
        except ValueError:
            response = f"[{len(exchange.response_content)} bytes {exchange.response_headers.get('content-type', '')}]"
        self._scrub.sent(params)
        self._scrub.sent(exchange.request_body)
        self._scrub.learn(response)
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
