"""The one way out of SkillForge: an HTTP client bound to one integration's base URL, host allowlist and mode.

The only module in ``app/`` that imports httpx (``tests/integrations/test_integrations_architecture.py``).
Every request passes ``_GuardedTransport``, which refuses a URL outside the base URL and the host allowlist and,
under pytest, any real network. ``write`` sends nothing in ``dry_run`` and returns the ``PlannedRequest`` instead;
a form POST that changes nothing - a token request - is a read (``read_form``) and goes out in every mode.
Nothing is retried and no redirect is followed: a failed read may be repeated by its caller, a write never is - a
write that may have reached the remote system is an ``OutcomeUnknownError`` for a later reconcile.
"""

import json as jsonlib
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from time import perf_counter
from types import TracebackType
from typing import Literal, Self

import httpx
from skillcore.logging.redaction import REDACTED

from app.core.logging import get_logger

from .config import IntegrationAccount, IntegrationMode
from .errors import (
    IntegrationError,
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
_ROUTE_WORD = re.compile(r"[A-Za-z_-]*|v[0-9]+")
"""A path segment that names a route, not an object: letters only, or an API version such as ``v1``."""

logger = get_logger(__name__)

# httpx logs each request's full URL, query included, at INFO, and httpcore its headers at DEBUG: neither may reach a
# log, so the one line per request is ``integration_request`` below.
for _library in ("httpx", "httpcore"):
    logging.getLogger(_library).setLevel(logging.WARNING)


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
        """The body parsed as JSON, a number with a fraction as ``Decimal``: no amount passes through ``float``.

        A ``ValueError`` if the body is no JSON; a ``RecursionError`` if it nests deeper than the parser goes.
        """
        return jsonlib.loads(self.content, parse_float=Decimal)


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


def path_template(path: str) -> str:
    """The path as a template: every segment that is no route word becomes ``{id}``, so no id reaches a log line.

    A path may carry the id of a person's record (``/Contact/4711``); a log line and an error message name
    ``/Contact/{id}`` instead.
    """
    return "/".join(segment if _ROUTE_WORD.fullmatch(segment) else "{id}" for segment in path.split("/"))


@dataclass(frozen=True, slots=True)
class _Context:
    """How a sent request is named and handled while its answer is read."""

    where: str
    path: str
    started: float
    observe: bool


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
            raise IntegrationRefusedError(f"path {path_template(url.path)!r} lies outside the base URL")

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

    async def read(self, path: str, *, params: Params | None = None, template: str | None = None) -> Reply:
        """Send a GET; it goes out in every mode.

        ``template`` names the path in the log line and in errors instead of ``path_template``, for a path whose
        object segments a route word could pass for (a mailbox, a calendar id).
        """
        request = self._client.build_request("GET", path, params=params)
        return await self._send(request, body=None, write=False, template=template)

    async def read_form(self, path: str, *, form: Mapping[str, str], template: str | None = None) -> Reply:
        """Send a form POST that changes nothing - a token request: a read, so it goes out in every mode.

        The form and the answer carry credentials: neither reaches the observer, a log line or an error.
        """
        request = self._client.build_request("POST", path, data=form)
        return await self._send(request, body=None, write=False, template=template, observe=False)

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

    async def _send(
        self, request: httpx.Request, *, body: object, write: bool, template: str | None = None, observe: bool = True
    ) -> Reply:
        started = perf_counter()
        path = template or path_template(request.url.path)
        where = f"{self._name} {request.method} {path}"
        # Every error is raised after its `except` block, never inside it: an error raised there would keep the httpx
        # exception as its (suppressed) context - and with it the request, its headers and the form it sent.
        failure: IntegrationError | None = None
        try:
            response = await self._client.send(request)
        except IntegrationRefusedError:
            raise
        except httpx.RequestError as exc:
            name = type(exc).__name__
            self._log(request, outcome=name, started=started, path=path)
            failure = (
                OutcomeUnknownError(f"{where} failed in transit ({name}); its outcome is unknown")
                if write
                else IntegrationUnavailableError(f"{where} failed in transit ({name})")
            )
        except Exception as exc:
            if not write:
                raise
            name = type(exc).__name__
            self._log(request, outcome=name, started=started, path=path)
            failure = OutcomeUnknownError(f"{where} failed ({name}); its outcome is unknown")
        if failure is not None:
            raise failure from None

        context = _Context(where=where, path=path, started=started, observe=observe)
        if not write:
            return self._answer(request, body, response, context, write=False)
        try:
            return self._answer(request, body, response, context, write=True)
        except IntegrationError:
            raise
        except Exception as exc:
            # The write went out: whatever fails while its answer is handled leaves its outcome unknown.
            failure = OutcomeUnknownError(
                f"{where} was sent, but its answer failed ({type(exc).__name__}); its outcome is unknown",
                status=response.status_code,
            )
        raise failure from None

    def _answer(
        self, request: httpx.Request, body: object, response: httpx.Response, context: _Context, *, write: bool
    ) -> Reply:
        self._log(request, outcome="response", started=context.started, status=response.status_code, path=context.path)
        if context.observe:
            self._observe(request, body, response)
        reply = Reply(response.status_code, response.content)
        if response.is_success:
            return reply

        where, remote, retry_after = context.where, self._remote_message(reply), response.headers.get("Retry-After")
        if response.is_client_error:
            error, message = IntegrationRejectedError, f"{where} was rejected"
        elif write:
            error, message = OutcomeUnknownError, f"{where} failed; its outcome is unknown"
        else:
            error, message = IntegrationUnavailableError, f"{where} failed"
        raise error(message, status=reply.status, remote_message=remote, retry_after=retry_after)

    def _remote_message(self, reply: Reply) -> str | None:
        """The remote system's error text; a failure to extract it is logged and never hides the error itself."""
        if self._error_message is None:
            return None
        try:
            return self._error_message(reply)
        except Exception as exc:
            logger.warning("integration_error_message_failed", integration=self._name, error=type(exc).__name__)
            return None

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
        self,
        request: httpx.Request,
        *,
        outcome: str,
        started: float | None = None,
        status: int | None = None,
        path: str | None = None,
    ) -> None:
        """One line per request: never a header, a query or a body - they may hold the credential or personal data."""
        logger.info(
            "integration_request",
            integration=self._name,
            method=request.method,
            path=path or path_template(request.url.path),
            outcome=outcome,
            status=status,
            duration_ms=None if started is None else round((perf_counter() - started) * 1000),
        )
