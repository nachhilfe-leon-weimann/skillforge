"""The one way out of SkillForge: an HTTP client bound to one integration's base URL, host allowlist and mode.

The only module in ``app/`` that imports httpx (``tests/integrations/test_integrations_architecture.py``).
Every request passes ``_GuardedTransport``, which refuses a URL outside the base URL and the host allowlist and,
under pytest, any real network. ``write`` sends nothing in ``dry_run`` and returns the ``PlannedRequest`` instead.
Nothing is retried and no redirect is followed: a failed read may be repeated by its caller, a write never is - a
write that may have reached the remote system is an ``OutcomeUnknownError`` for a later reconcile.
"""

import json as jsonlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import perf_counter
from types import TracebackType
from typing import Literal, Self

import httpx
from skillcore.logging.redaction import REDACTED

from app.core.logging import get_logger

from .config import IntegrationAccount, IntegrationMode
from .errors import (
    IntegrationRefusedError,
    IntegrationRejectedError,
    IntegrationUnavailableError,
    OutcomeUnknownError,
)
from .guards import ensure_live_allowed, ensure_network_allowed

type InnerTransport = httpx.AsyncBaseTransport
type Params = Mapping[str, str | int | bool]
type WriteMethod = Literal["POST", "PUT", "DELETE"]

DEFAULT_TIMEOUT_SECONDS = 30.0
NETWORK_RETRIES = 0
"""httpx retries a failed connect this many times; never, so a write cannot go out twice."""
_COOKIE_HEADERS = frozenset({"cookie", "set-cookie"})

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PlannedRequest:
    """A write that ``dry_run`` did not send: what would have gone out, headers left out."""

    method: str
    url: str
    body: object = None


@dataclass(frozen=True, slots=True)
class Reply:
    """A successful response."""

    status: int
    content: bytes = field(repr=False)

    def json(self) -> object:
        """The body parsed as JSON; a ``ValueError`` if it is none."""
        return jsonlib.loads(self.content)


@dataclass(frozen=True, slots=True)
class Exchange:
    """One request and its response, as an observer such as the recorder sees it: credentials and cookies redacted."""

    method: str
    url: str
    request_headers: dict[str, str]
    request_body: object
    status: int
    response_headers: dict[str, str]
    response_content: bytes = field(repr=False)


type Observer = Callable[[Exchange], None]
type ErrorMessage = Callable[[Reply], str | None]


def network_transport() -> httpx.AsyncHTTPTransport:
    """The transport to the network: no retries."""
    return httpx.AsyncHTTPTransport(retries=NETWORK_RETRIES)


class _GuardedTransport(httpx.AsyncBaseTransport):
    """Hands a request to the inner transport only if its URL is allowed and the network may be used."""

    def __init__(
        self, inner: InnerTransport, *, base_url: httpx.URL, allowed_hosts: frozenset[str], account: IntegrationAccount
    ) -> None:
        self._inner = inner
        self._base_path = base_url.path.rstrip("/") + "/"
        self._allowed_hosts = allowed_hosts
        self._account = account

    def check(self, request: httpx.Request) -> None:
        url = request.url
        if url.scheme != "https" or url.host not in self._allowed_hosts or url.port is not None or url.userinfo:
            raise IntegrationRefusedError(f"host {url.host!r} is not on the allowlist")
        segments = url.path.split("/")
        if not url.path.startswith(self._base_path) or "." in segments or ".." in segments:
            raise IntegrationRefusedError(f"path {url.path!r} lies outside the base URL")

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.check(request)
        if not isinstance(self._inner, httpx.MockTransport):
            ensure_network_allowed(self._account)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class IntegrationHttp:
    """An integration's HTTP client, used as ``async with``: it opens on enter and closes on exit.

    ``credentials`` are headers such as ``Authorization``: they go into no log, no error, no ``repr`` and no
    ``Exchange``. ``error_message`` extracts the remote system's error text from an error response. ``transport``
    is for tests (``httpx.MockTransport``); without it the request goes to the network.
    """

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        allowed_hosts: frozenset[str],
        mode: IntegrationMode,
        account: IntegrationAccount,
        headers: Mapping[str, str] | None = None,
        credentials: Mapping[str, str] | None = None,
        error_message: ErrorMessage | None = None,
        observer: Observer | None = None,
        transport: InnerTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if mode is IntegrationMode.LIVE:
            ensure_live_allowed(account)
        self._name = name
        self._mode = mode
        self._base_url = httpx.URL(base_url)
        self._headers = {**(headers or {}), **(credentials or {})}
        self._redacted = _COOKIE_HEADERS | {header.lower() for header in credentials or {}}
        self._error_message = error_message
        self._observer = observer
        self._guard = _GuardedTransport(
            transport if transport is not None else network_transport(),
            base_url=self._base_url,
            allowed_hosts=allowed_hosts,
            account=account,
        )
        self._timeout = timeout
        self._opened: httpx.AsyncClient | None = None

    def __repr__(self) -> str:
        return f"IntegrationHttp(name={self._name!r}, mode={self._mode.value!r})"

    async def __aenter__(self) -> Self:
        self._opened = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            transport=self._guard,
            timeout=self._timeout,
            follow_redirects=False,
        )
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        if self._opened is not None:
            await self._opened.aclose()
            self._opened = None

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._opened is None:
            raise RuntimeError(f"{self!r} is used outside its `async with` block")
        return self._opened

    async def read(self, path: str, *, params: Params | None = None) -> Reply:
        """Send a GET; it goes out in every mode."""
        request = self._client.build_request("GET", path, params=params)
        return await self._send(request, body=None, write=False)

    async def write(
        self, method: WriteMethod, path: str, *, params: Params | None = None, json: object = None
    ) -> Reply | PlannedRequest:
        """Send a write in ``live`` mode; in ``dry_run`` return what would have been sent."""
        request = self._client.build_request(method, path, params=params, json=json)
        if self._mode is IntegrationMode.DRY_RUN:
            self._guard.check(request)
            self._log(request, outcome="planned")
            return PlannedRequest(method, str(request.url), json)
        return await self._send(request, body=json, write=True)

    async def _send(self, request: httpx.Request, *, body: object, write: bool) -> Reply:
        started = perf_counter()
        where = f"{self._name} {request.method} {request.url.path}"
        try:
            response = await self._client.send(request)
        except httpx.RequestError as exc:
            # No chaining: an httpx error can carry the request, its headers and a header value in its message.
            failure = type(exc).__name__
            self._log(request, outcome=failure, started=started)
            if write:
                raise OutcomeUnknownError(f"{where} failed in transit ({failure}); its outcome is unknown") from None
            raise IntegrationUnavailableError(f"{where} failed in transit ({failure})") from None

        self._log(request, outcome="response", started=started, status=response.status_code)
        self._observe(request, body, response)
        reply = Reply(response.status_code, response.content)
        if response.is_success:
            return reply

        remote_message = self._error_message(reply) if self._error_message else None
        if response.is_client_error:
            raise IntegrationRejectedError(f"{where} was rejected", status=reply.status, remote_message=remote_message)
        if write:
            raise OutcomeUnknownError(
                f"{where} failed; its outcome is unknown", status=reply.status, remote_message=remote_message
            )
        raise IntegrationUnavailableError(f"{where} failed", status=reply.status, remote_message=remote_message)

    def _observe(self, request: httpx.Request, body: object, response: httpx.Response) -> None:
        """Hand the exchange to the observer; its failure is logged and never hides the outcome of the call."""
        if self._observer is None:
            return
        exchange = Exchange(
            method=request.method,
            url=str(request.url),
            request_headers=self._redact(request.headers),
            request_body=body,
            status=response.status_code,
            response_headers=self._redact(response.headers),
            response_content=response.content,
        )
        try:
            self._observer(exchange)
        except Exception as exc:
            logger.warning("integration_observer_failed", integration=self._name, error=type(exc).__name__)

    def _redact(self, headers: httpx.Headers) -> dict[str, str]:
        return {name: REDACTED if name.lower() in self._redacted else value for name, value in headers.items()}

    def _log(
        self, request: httpx.Request, *, outcome: str, started: float | None = None, status: int | None = None
    ) -> None:
        """One line per request: never a header, a query or a body - they may hold the credential or personal data."""
        logger.info(
            "integration_request",
            integration=self._name,
            method=request.method,
            path=request.url.path,
            outcome=outcome,
            status=status,
            duration_ms=None if started is None else round((perf_counter() - started) * 1000),
        )
