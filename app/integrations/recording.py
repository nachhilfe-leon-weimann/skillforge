"""The recorder: writes request/response pairs as JSON fixtures, scrubbed of anything that identifies someone.

It is an ``Observer`` of ``IntegrationHttp``. The scrub removes credentials and cookies, the fields of people and
companies (names of accounts, contacts and users, addresses, bank, tax and contact details), and in every string
e-mail addresses (to ``example.org``), IBANs, phone numbers, 32-hex tokens, the trial marker and every value it
scrubbed before. Large blobs such as a PDF are replaced by their size. ``tests/test_fixture_scan.py`` checks
the result independently. Recorded fixtures are never edited by hand.
"""

import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .transport import Exchange

SCRUBBED = "[SCRUBBED]"
MAX_STRING = 2000
"""Longer strings - a PDF or an image as base64 - are recorded by size only."""

_RECORDED_HEADERS = frozenset({"content-type", "accept", "user-agent"})
"""Only headers that describe the format are kept: credentials and cookies never reach a fixture."""
_PERSONAL_KEYS = frozenset({
    "academictitle",
    "address",
    "addressname",
    "addressname2",
    "addressstreet",
    "addresszip",
    "addresscity",
    "bankaccount",
    "banknumber",
    "bic",
    "birthday",
    "city",
    "email",
    "familyname",
    "fax",
    "firstname",
    "fullname",
    "iban",
    "lastname",
    "mobile",
    "name2",
    "phone",
    "street",
    "surename",
    "taxnumber",
    "titel",
    "username",
    "vatnumber",
    "zip",
})
_NAMED_OBJECTS = frozenset({"SevClient", "SevUser", "Contact", "ContactAddress", "CommunicationWay"})
"""Objects whose ``name`` (and ``value``) belongs to a company or a person."""
_ACCOUNT = {"id": "0", "objectName": "SevClient"}
"""The account every object belongs to; recorded as a placeholder."""

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
_PHONE = re.compile(r"(?<![\w.:-])(?:\+|0)\d[\d /-]{6,}\d(?![\w.:-])")
_HEX_TOKEN = re.compile(r"\b[0-9a-fA-F]{32}\b")
TRIAL_MARKER = "TRIAL"


def _normalized(key: str) -> str:
    return key.replace("-", "").replace("_", "").lower()


class Scrubber:
    """Scrubs JSON-like data; every value it scrubbed by key is also replaced wherever it shows up later."""

    def __init__(self) -> None:
        self._known: set[str] = set()

    def __call__(self, data: object) -> object:
        self._learn(data)
        return self._scrub(data)

    def text(self, value: str) -> str:
        if len(value) > MAX_STRING:
            return f"[{len(value)} characters]"
        value = _EMAIL.sub(lambda match: match[0] if match[0].endswith("@example.org") else "person@example.org", value)
        value = _IBAN.sub("[IBAN]", value)
        value = _PHONE.sub("[PHONE]", value)
        value = _HEX_TOKEN.sub("[TOKEN]", value)
        for known in sorted(self._known, key=len, reverse=True):
            value = value.replace(known, SCRUBBED)
        return value.replace(TRIAL_MARKER, "[MARKER]")

    def _sensitive(self, key: str, parent: dict[str, object]) -> bool:
        normalized = _normalized(key)
        if normalized in _PERSONAL_KEYS:
            return True
        return parent.get("objectName") in _NAMED_OBJECTS and normalized in {"name", "value"}

    def _learn(self, data: object) -> None:
        match data:
            case dict():
                for key, value in data.items():
                    if isinstance(value, str) and len(value) >= 3 and self._sensitive(key, data):
                        self._known.add(value)
                    self._learn(value)
            case list():
                for item in data:
                    self._learn(item)

    def _scrub(self, data: object) -> object:
        match data:
            case dict():
                return {key: self._scrub_entry(key, value, data) for key, value in data.items()}
            case list():
                return [self._scrub(item) for item in data]
            case str():
                return self.text(data)
        return data

    def _scrub_entry(self, key: str, value: object, parent: dict[str, object]) -> object:
        if value is None:
            return None
        if self._sensitive(key, parent):
            return SCRUBBED
        if key == "sevClient":
            return _ACCOUNT
        if key == "id" and parent.get("objectName") == "SevClient":
            return _ACCOUNT["id"]
        return self._scrub(value)


class Recorder:
    """An observer that writes each exchange to ``<directory>/<step>-<NN>.json``; set ``step`` before each step."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.step = "exchange"
        self._scrub = Scrubber()
        self._count = 0

    def __call__(self, exchange: Exchange) -> None:
        self._count += 1
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{self.step}-{self._count:02d}.json"
        path.write_text(json.dumps(self.record(exchange), indent=2, ensure_ascii=False) + "\n")

    def record(self, exchange: Exchange) -> dict[str, object]:
        url = urlsplit(exchange.url)
        return {
            "request": {
                "method": exchange.method,
                "path": url.path,
                "params": self._scrub(dict(parse_qsl(url.query))),
                "headers": _headers(exchange.request_headers),
                "body": self._scrub(exchange.request_body),
            },
            "response": {
                "status": exchange.status,
                "headers": _headers(exchange.response_headers),
                "body": self._body(exchange),
            },
        }

    def _body(self, exchange: Exchange) -> object:
        try:
            return self._scrub(json.loads(exchange.response_content))
        except ValueError:
            return f"[{len(exchange.response_content)} bytes {exchange.response_headers.get('content-type', '')}]"


def _headers(headers: dict[str, str]) -> dict[str, str]:
    return {name: value for name, value in headers.items() if name.lower() in _RECORDED_HEADERS}
