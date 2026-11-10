"""The one way out of SkillForge: an HTTP client bound to one integration's base URL, host allowlist and mode.

The only module in ``app/`` that imports httpx (``tests/integrations/test_integrations_architecture.py``).
Every request passes ``_GuardedTransport``, which refuses a host outside the allowlist and, under pytest,
any real network. ``write`` sends nothing in ``dry_run`` and returns the ``PlannedRequest`` instead.
Nothing is retried: a failed read may be repeated by its caller, a write never is - a write that may have
reached the remote system is an ``OutcomeUnknownError`` for a later reconcile.
"""

import json as jsonlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import perf_counter
from types import TracebackType
from typing import Literal, Self

import httpx

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
REDACTED = "[REDACTED]"
_SECRET_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "set-cookie"})

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
    content_type: str
    content: bytes = field(repr=False)

    def json(self) -> object:
        """The body parsed as JSON; a ``ValueError`` if it is none."""
        return jsonlib.loads(self.content)


@dataclass(frozen=True, slots=True)
class Exchange:
    """One request and its response, as an observer such as the recorder sees it: secret headers redacted."""

    method: str
    url: str
    request_headers: dict[str, str]
    request_body: object
    status: int
    response_headers: dict[str, str]
    response_content: bytes = field(repr=False)


type Observer = Callable[[Exchange], None]
type ErrorMessage = Callable[[Reply], str | None]


class _GuardedTransport(httpx.AsyncBaseTransport):
    """Hands a request to the inner transport only if its host is allowed and the network may be used."""

    def __init__(self, inner: InnerTransport, allowed_hosts: frozenset[str]) -> None:
        self._inner = inner
        self._allowed_hosts = allowed_hosts

    def check(self, request: httpx.Request) -> None:
        if request.url.scheme != "https" or request.url.host not in self._allowed_hosts:
            raise IntegrationRefusedError(f"host {request.url.host!r} is not on the allowlist")

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.check(request)
        if not isinstance(self._inner, httpx.MockTransport):
            ensure_network_allowed()
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class IntegrationHttp:
    """An integration's HTTP client. Construct it per unit of work and close it (``async with``).

    ``headers`` may hold the credential: it goes into no log, no error, no ``repr`` and no ``Exchange``.
    ``error_message`` extracts the remote system's error text from an error response. ``transport`` is
    for tests (``httpx.MockTransport``); without it the request goes to the network.
    """

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        allowed_hosts: frozenset[str],
        headers: Mapping[str, str],
        mode: IntegrationMode,
        account: IntegrationAccount,
        error_message: ErrorMessage | None = None,
        observer: Observer | None = None,
        transport: InnerTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if mode is IntegrationMode.LIVE:
            ensure_live_allowed(account)
        self._name = name
        self._mode = mode
        self._error_message = error_message
        self._observer = observer
        inner = transport if transport is not None else httpx.AsyncHTTPTransport(retries=0)
        self._guard = _GuardedTransport(inner, allowed_hosts)
        self._client = httpx.AsyncClient(
            base_url=base_url, headers=dict(headers), transport=self._guard, timeout=timeout, follow_redirects=False
        )

    def __repr__(self) -> str:
        return f"IntegrationHttp(name={self._name!r}, mode={self._mode.value!r})"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self._client.aclose()

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
            self._log(request, outcome="transport_error", started=started)
            if write:
                raise OutcomeUnknownError(f"{where} failed in transit; its outcome is unknown") from exc
            raise IntegrationUnavailableError(f"{where} failed in transit") from exc

        reply = Reply(response.status_code, response.headers.get("content-type", ""), response.content)
        if self._observer is not None:
            self._observer(_exchange(request, body, response))
        self._log(request, outcome="response", started=started, status=reply.status)
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

    def _log(self, request: httpx.Request, *, outcome: str, started: float | None = None, status: int = 0) -> None:
        """One line per request: never a header, a query or a body - they may hold the credential or personal data."""
        logger.info(
            "integration_request",
            integration=self._name,
            method=request.method,
            path=request.url.path,
            outcome=outcome,
            status=status or None,
            duration_ms=None if started is None else round((perf_counter() - started) * 1000),
        )


def _exchange(request: httpx.Request, body: object, response: httpx.Response) -> Exchange:
    return Exchange(
        method=request.method,
        url=str(request.url),
        request_headers=_redacted(request.headers),
        request_body=body,
        status=response.status_code,
        response_headers=_redacted(response.headers),
        response_content=response.content,
    )


def _redacted(headers: httpx.Headers) -> dict[str, str]:
    return {name: REDACTED if name.lower() in _SECRET_HEADERS else value for name, value in headers.items()}
