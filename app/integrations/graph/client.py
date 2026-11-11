"""The Graph client: the one lesson calendar, read through ``calendarView`` - nothing else of Microsoft 365.

A client comes from ``GraphClient.connect``: it takes the app's token (``credential.py``) and confirms the lesson
calendar (``identity.py``); until then it reads no calendar. Graph pages with an absolute ``@odata.nextLink``, which
is followed through the same guarded transport and so must stay on the Graph host. Times are asked for in UTC,
and only the fields of fixture G1 - never a body, attendees or an organizer. A log line and an error name a path
by its template (``CALENDAR_VIEW_TEMPLATE``), never the mailbox or a calendar id.

A ``429`` is repeated as its ``Retry-After`` says - by this client, for reads only, at most ``MAX_ATTEMPTS`` times
and never after a wait above ``MAX_RETRY_AFTER_SECONDS``; a ``Retry-After`` that is missing, no whole number of
seconds or above the cap ends the read as ``IntegrationUnavailableError``. Any other error, a ``403`` among them,
is never repeated. One read - a ``connect`` block: the token request, the identity check and the ``calendarView``
pages - ends within ``READ_DEADLINE_SECONDS``: no send starts and no wait is taken without time left, a send still
running at the deadline is cut, and the read ends as ``IntegrationUnavailableError``. A listing longer than
``MAX_PAGES`` is refused, never cut short. ``GRAPH__MODE`` decides nothing yet: the client has no write.
"""

import asyncio
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self
from urllib.parse import quote

from pydantic import Field, ValidationError

from app.integrations.config import IntegrationAccount
from app.integrations.errors import (
    IdentityCheckError,
    IntegrationRefusedError,
    IntegrationRejectedError,
    IntegrationUnavailableError,
    UnexpectedResponseError,
)
from app.integrations.transport import InnerTransport, IntegrationHttp, Params, Reply

from .config import GraphSettings
from .credential import TOKEN_PATH_TEMPLATE, TOKENS, AppCredential, TokenCache, app_token, load_credential
from .identity import verify_calendar
from .models import Calendar, CalendarEvent, GraphModel

GRAPH_HOST = "graph.microsoft.com"
BASE_URL = f"https://{GRAPH_HOST}/v1.0"
USER_AGENT = "skillforge"
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
"""The fields of fixture G1, in its order: what the import needs of an event, nothing more."""
CALENDAR_FIELDS = ("id", "name", "owner")
PAGE_SIZE = 100
MAX_PAGES = 100
"""A listing longer than this many pages is refused rather than read without end."""
MAX_ATTEMPTS = 3
"""A throttled read is sent at most this often."""
MAX_RETRY_AFTER_SECONDS = 30
READ_DEADLINE_SECONDS = 120
"""One read - token, identity check and every page - ends within this many seconds, throttling waits included."""
CALENDARS_TEMPLATE = "/users/{mailbox}/calendars"
CALENDAR_VIEW_TEMPLATE = "/users/{mailbox}/calendars/{calendar_id}/calendarView"

_SECONDS = re.compile(r"[0-9]+")
"""``Retry-After`` as delta-seconds; Graph sends no HTTP date, and one is not honoured."""

type Sleep = Callable[[float], Awaitable[None]]
type Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class _Deadline:
    """The end of one read, on a monotonic clock."""

    at: float
    clock: Clock

    @classmethod
    def start(cls, clock: Clock) -> _Deadline:
        return cls(clock() + READ_DEADLINE_SECONDS, clock)

    def left(self) -> float:
        return self.at - self.clock()

    def passed(self, where: str) -> IntegrationUnavailableError:
        return IntegrationUnavailableError(f"graph {where}: the read passed its deadline of {READ_DEADLINE_SECONDS} s")

    async def run[T](self, send: Callable[[], Awaitable[T]], where: str) -> T:
        """Start ``send`` only with time left, and cut it at the deadline."""
        if (left := self.left()) > 0:
            try:
                async with asyncio.timeout(left) as scope:
                    return await send()
            except TimeoutError:
                if not scope.expired():
                    raise
        raise self.passed(where) from None


class GraphClient:
    """The lesson calendar of one mailbox. Built by ``connect``: a client whose calendar is not confirmed reads none
    (``IdentityCheckError``)."""

    def __init__(
        self,
        http: IntegrationHttp,
        *,
        mailbox: str,
        sleep: Sleep = asyncio.sleep,
        deadline: _Deadline | None = None,
    ) -> None:
        self._http = http
        self._user_path = f"/users/{quote(mailbox, safe='@')}"
        self._sleep = sleep
        self._deadline = deadline or _Deadline.start(time.monotonic)
        self._calendar_id: str | None = None

    def __repr__(self) -> str:
        return f"GraphClient(confirmed={self._calendar_id is not None})"

    @classmethod
    @asynccontextmanager
    async def connect(
        cls,
        settings: GraphSettings,
        *,
        transport: InnerTransport | None = None,
        tokens: TokenCache = TOKENS,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> AsyncGenerator[Self]:
        """A client whose lesson calendar passed the identity check (fail closed: ``IdentityCheckError``), for one
        read: its deadline starts here."""
        deadline = _Deadline.start(clock)
        credential = _credential(settings)
        if settings.mailbox is None or settings.calendar_name is None:
            raise IdentityCheckError("GRAPH__MAILBOX or GRAPH__CALENDAR_NAME is not set: no calendar can be confirmed")
        token = await deadline.run(
            lambda: app_token(credential, mode=settings.mode, tokens=tokens, transport=transport),
            f"POST {TOKEN_PATH_TEMPLATE}",
        )
        http = IntegrationHttp(
            name="graph",
            base_url=BASE_URL,
            allowed_hosts=frozenset({GRAPH_HOST}),
            headers={"Accept": "application/json", "User-Agent": USER_AGENT, "Prefer": 'outlook.timezone="UTC"'},
            credentials={"Authorization": f"Bearer {token.get_secret_value()}"},
            mode=settings.mode,
            account=IntegrationAccount.LIVE,
            error_message=graph_error,
            transport=transport,
        )
        async with http:
            client = cls(http, mailbox=settings.mailbox, sleep=sleep, deadline=deadline)
            client._calendar_id = await verify_calendar(
                client._calendars, mailbox=settings.mailbox, name=settings.calendar_name
            )
            yield client

    async def calendar_view(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Every occurrence in the lesson calendar from ``start`` to ``end``, series expanded, read to the last page."""
        if self._calendar_id is None:
            raise IdentityCheckError("the lesson calendar is not confirmed: a client comes from GraphClient.connect")
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError("calendar_view takes an aware start before an aware end")
        params = {
            "startDateTime": _instant(start),
            "endDateTime": _instant(end),
            "$select": ",".join(EVENT_FIELDS),
            "$top": PAGE_SIZE,
        }
        path = f"{self._user_path}/calendars/{quote(self._calendar_id, safe='')}/calendarView"
        items = await self._read_all(path, params, CALENDAR_VIEW_TEMPLATE)
        return [_validated(CalendarEvent, item, CALENDAR_VIEW_TEMPLATE) for item in items]

    async def _calendars(self) -> list[Calendar]:
        """The mailbox's calendars with their owners: the identity read."""
        params = {"$select": ",".join(CALENDAR_FIELDS)}
        items = await self._read_all(f"{self._user_path}/calendars", params, CALENDARS_TEMPLATE)
        return [_validated(Calendar, item, CALENDARS_TEMPLATE) for item in items]

    async def _read_all(self, path: str, params: Params, template: str) -> list[object]:
        """Every page of a listing, by ``@odata.nextLink``, which carries the parameters of every later page."""
        items: list[object] = []
        url, page_params = path, params
        for _ in range(MAX_PAGES):
            page = _validated(_Page, _json(await self._read(url, page_params, template), template), template)
            items.extend(page.value)
            if page.next_link is None:
                return items
            url, page_params = page.next_link, None
        raise UnexpectedResponseError(f"graph GET {template}: more than {MAX_PAGES} pages")

    async def _read(self, url: str, params: Params | None, template: str) -> Reply:
        """One read; a ``429`` is repeated as its ``Retry-After`` says, within ``MAX_ATTEMPTS``, the cap and the
        read's deadline."""
        where = f"GET {template}"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return await self._deadline.run(lambda: self._http.read(url, params=params, template=template), where)
            except IntegrationRejectedError as exc:
                if exc.status != 429:
                    raise
                delay = _retry_after(exc.retry_after)
                if delay is None or attempt == MAX_ATTEMPTS:
                    raise IntegrationUnavailableError(
                        f"graph GET {template} was throttled ({attempt} of {MAX_ATTEMPTS} attempts)", status=429
                    ) from None
                if delay >= self._deadline.left():
                    raise self._deadline.passed(where) from None
                await self._sleep(delay)
        raise AssertionError("unreachable")


class _Page(GraphModel):
    value: list[object]
    next_link: str | None = Field(default=None, alias="@odata.nextLink")


def _credential(settings: GraphSettings) -> AppCredential:
    """The app's credential from the settings; refused before anything is sent if a part is not set."""
    tenant, client, certificate, key = (
        settings.tenant_id,
        settings.client_id,
        settings.certificate_pem,
        settings.private_key_pem,
    )
    if tenant is None or client is None or certificate is None or key is None:
        parts = {"TENANT_ID": tenant, "CLIENT_ID": client, "CERTIFICATE_PEM": certificate, "PRIVATE_KEY_PEM": key}
        missing = ", ".join(f"GRAPH__{name}" for name, value in parts.items() if value is None)
        raise IntegrationRefusedError(f"not set: {missing}")
    return load_credential(
        tenant_id=str(tenant), client_id=str(client), certificate_pem=certificate, private_key_pem=key
    )


def _instant(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _retry_after(value: str | None) -> int | None:
    """The wait a ``429`` asks for, in whole seconds - ``None`` if it gives none this client honours."""
    if value is None or not _SECONDS.fullmatch(value.strip()):
        return None
    seconds = int(value)
    return seconds if seconds <= MAX_RETRY_AFTER_SECONDS else None


def graph_error(reply: Reply) -> str | None:
    """Graph's error code - never its message, which may name a mailbox."""
    try:
        body = reply.json()
    except ValueError:
        return None
    match body:
        case {"error": {"code": str(code)}}:
            return code
    return None


def _json(reply: Reply, template: str) -> object:
    try:
        return reply.json()
    except ValueError, RecursionError:  # a body nested too deep for the parser
        raise UnexpectedResponseError(f"graph GET {template}: the response is not JSON") from None


def _validated[M: GraphModel](model: type[M], data: object, template: str) -> M:
    """Validate a response; the error names the fields, never their values, which may be a name or a title."""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        fields = sorted({".".join(map(str, error["loc"])) for error in exc.errors()})
        raise UnexpectedResponseError(f"graph GET {template}: unexpected response at {fields}") from None
