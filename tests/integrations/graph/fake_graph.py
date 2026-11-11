"""A synthetic Microsoft Graph and login endpoint behind ``httpx.MockTransport``, answering from fixtures G1 to G7.

Every request is remembered; an answer can be a response, an exception, a callable or a queue of them, of which
the last one stays. The fixtures are synthetic (``tests/fixtures/graph/README.md``).
"""

import base64
import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "graph"
LOGIN_HOST = "login.microsoftonline.com"
GRAPH_HOST = "graph.microsoft.com"

type Answer = httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]


@cache
def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def answer(name: str) -> httpx.Response:
    """The recorded response of a fixture."""
    response = fixture(name)["response"]
    return httpx.Response(response["status"], json=response["body"], headers=response["headers"])


TOKEN_REQUEST = fixture("g7-token-response")["request"]
TENANT_ID = TOKEN_REQUEST["path"].split("/")[1]
CLIENT_ID = TOKEN_REQUEST["body"]["client_id"]
ACCESS_TOKEN = fixture("g7-token-response")["response"]["body"]["access_token"]
CALENDARS = fixture("g1-calendars")
MAILBOX = CALENDARS["request"]["path"].split("/")[3]
CALENDAR_NAME, CALENDAR_ID = next(
    (calendar["name"], calendar["id"])
    for calendar in CALENDARS["response"]["body"]["value"]
    if calendar["name"] == "Unterricht"
)
VIEW = fixture("g1-calendar-view-page-1")["request"]
VIEW_PATH = VIEW["path"]
WINDOW = tuple(dt.datetime.fromisoformat(VIEW["params"][key]) for key in ("startDateTime", "endDateTime"))


def calendar_view(request: httpx.Request) -> httpx.Response:
    """G1's two pages: the second is the one ``@odata.nextLink`` of the first names."""
    return answer("g1-calendar-view-page-2" if request.url.params.get("$skip") == "3" else "g1-calendar-view-page-1")


def with_next_link(name: str, next_link: str | None) -> httpx.Response:
    body = {**fixture(name)["response"]["body"], "@odata.nextLink": next_link}
    return httpx.Response(200, json={key: value for key, value in body.items() if value is not None})


class FakeGraph:
    """Answers ``(method, path)`` routes of both hosts; an unknown route is Graph's 404."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.routes: dict[tuple[str, str], list[Answer]] = {}
        self.on("POST", TOKEN_REQUEST["path"], answer("g7-token-response"))
        self.on("GET", CALENDARS["request"]["path"], answer("g1-calendars"))
        self.on("GET", VIEW_PATH, calendar_view)

    def on(self, method: str, path: str, *answers: Answer) -> None:
        self.routes[(method, path)] = list(answers)

    def sent(self) -> list[str]:
        return [f"{request.method} {request.url.host}{request.url.path}" for request in self.requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.routes.get((request.method, request.url.path))
        if not queue:
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound", "message": "Not found"}})
        found = queue.pop(0) if len(queue) > 1 else queue[0]
        match found:
            case Exception():
                raise found
            case httpx.Response():
                return found
            case _:
                return found(request)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def form(request: httpx.Request) -> dict[str, str]:
    return dict(httpx.QueryParams(request.content.decode()))


def host(url: str) -> str | None:
    return urlsplit(url).hostname


@dataclass(frozen=True)
class Credential:
    """A throwaway key and self-signed certificate, made at test time: no key enters the repository."""

    certificate: x509.Certificate
    key: rsa.RSAPrivateKey

    @classmethod
    def generate(cls) -> Credential:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "skillforge synthetic test")])
        now = dt.datetime.now(dt.UTC)
        certificate = (
            x509
            .CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=1))
            .not_valid_after(now + dt.timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        return cls(certificate, key)

    @property
    def certificate_pem(self) -> str:
        return self.certificate.public_bytes(serialization.Encoding.PEM).decode()

    @property
    def key_pem(self) -> str:
        return self.key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()

    @property
    def certificate_setting(self) -> str:
        return base64.b64encode(self.certificate_pem.encode()).decode()

    @property
    def key_setting(self) -> str:
        return base64.b64encode(self.key_pem.encode()).decode()

    @property
    def thumbprint(self) -> str:
        return base64.urlsafe_b64encode(self.certificate.fingerprint(hashes.SHA1())).decode().rstrip("=")
