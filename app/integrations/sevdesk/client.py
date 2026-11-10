"""The sevDesk client: sevDesk's API as SkillForge uses it, nothing more.

sevDesk wraps a result in ``{"objects": ...}``, types numbers as strings, answers some validation errors with a
500 and ``{"error": {"message": ...}}``, and pages with ``limit`` (1 to 1000), ``offset`` and ``countAll``. A
client comes from ``SevdeskClient.connect``, which confirms the account; until then a client sends nothing but
the identity read.
"""

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Self

from pydantic import NonNegativeInt, ValidationError

from app.integrations.errors import (
    IdentityCheckError,
    IntegrationRefusedError,
    IntegrationRejectedError,
    UnexpectedResponseError,
)
from app.integrations.transport import InnerTransport, IntegrationHttp, Observer, Params, Reply

from .config import SevdeskSettings
from .identity import verify_account
from .models import (
    Account,
    BookkeepingVersion,
    Contact,
    ContactCustomField,
    CustomFieldSetting,
    Part,
    SevdeskModel,
    SevdeskObject,
    SevUser,
    Unity,
    unwrap_single,
)

BASE_URL = "https://my.sevdesk.de/api/v1"
ALLOWED_HOSTS = frozenset({"my.sevdesk.de"})
USER_AGENT = "skillforge"
PAGE_LIMIT = 100
"""Page size for listing reads; sevDesk accepts 1 to 1000."""
MAX_PAGES = 100
"""A listing longer than this many pages is refused rather than read without end."""


def _sevdesk_http(
    settings: SevdeskSettings, *, observer: Observer | None = None, transport: InnerTransport | None = None
) -> IntegrationHttp:
    """The guarded HTTP client for sevDesk; refused without a token."""
    if settings.token is None:
        raise IntegrationRefusedError("SEVDESK__TOKEN is not set")
    return IntegrationHttp(
        name="sevdesk",
        base_url=BASE_URL,
        allowed_hosts=ALLOWED_HOSTS,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        credentials={"Authorization": settings.token.get_secret_value()},
        mode=settings.mode,
        account=settings.account,
        error_message=error_message,
        observer=observer,
        transport=transport,
    )


class SevdeskClient:
    """sevDesk's API for one account. Built by ``connect``: a client whose identity check has not passed refuses
    every call but the identity read itself (``IdentityCheckError``)."""

    def __init__(self, http: IntegrationHttp, settings: SevdeskSettings) -> None:
        self._http = http
        self._settings = settings
        self._confirmed = False

    def __repr__(self) -> str:
        return f"SevdeskClient(mode={self._settings.mode.value!r}, account={self._settings.account.value!r})"

    @classmethod
    @asynccontextmanager
    async def connect(
        cls, settings: SevdeskSettings, *, observer: Observer | None = None, transport: InnerTransport | None = None
    ) -> AsyncGenerator[Self]:
        """A client whose account passed the identity check (fail closed: ``IdentityCheckError``)."""
        async with _sevdesk_http(settings, observer=observer, transport=transport) as http:
            client = cls(http, settings)
            await verify_account(client.account, settings)
            client._confirmed = True
            yield client

    # --- reads ---

    async def account(self) -> Account:
        """The account the token belongs to, read from ``GET /SevClient`` - undocumented; the spike confirms it."""
        where = "GET /SevClient"
        # The one call that goes out before the account is confirmed: it is how it gets confirmed.
        accounts = _many(Account, _objects(await self._http.read("/SevClient"), where), where)
        if len(accounts) != 1:
            raise UnexpectedResponseError(f"sevdesk GET /SevClient: expected one account, got {len(accounts)}")
        return accounts[0]

    async def bookkeeping_version(self) -> str:
        return (await self._get_one(BookkeepingVersion, "/Tools/bookkeepingSystemVersion")).version

    async def users(self) -> list[SevUser]:
        return await self._get_list(SevUser, "/SevUser")

    async def get_user(self, user_id: int) -> SevUser | None:
        return await self._get_by_id(SevUser, f"/SevUser/{user_id}")

    async def get_unity(self, unity_id: int) -> Unity | None:
        return await self._get_by_id(Unity, f"/Unity/{unity_id}")

    async def get_contact(self, contact_id: int) -> Contact | None:
        return await self._get_by_id(Contact, f"/Contact/{contact_id}")

    async def custom_field_settings(self) -> list[CustomFieldSetting]:
        return await self._get_list(CustomFieldSetting, "/ContactCustomFieldSetting")

    async def get_custom_field_setting(self, setting_id: int) -> CustomFieldSetting | None:
        return await self._get_by_id(CustomFieldSetting, f"/ContactCustomFieldSetting/{setting_id}")

    async def contact_custom_fields(self, contact_id: int) -> list[ContactCustomField]:
        """The custom field values of one contact; filtered here too, in case sevDesk ignores the filter.

        Whether sevDesk filters by contact is a spike question."""
        params = {"contact[id]": contact_id, "contact[objectName]": "Contact"}
        fields = await self._get_all(ContactCustomField, "/ContactCustomField", params)
        return [field for field in fields if field.contact.id == contact_id]

    async def find_contacts_by_custom_field(self, field_name: str, value: str) -> list[Contact]:
        params = {"customFieldName": field_name, "value": value}
        return await self._get_list(Contact, "/Contact/Factory/findContactsByCustomFieldValue", params)

    async def parts(self) -> list[Part]:
        return await self._get_all(Part, "/Part")

    # --- plumbing ---

    def _ensure_confirmed(self) -> None:
        if not self._confirmed:
            raise IdentityCheckError("the sevDesk account is not confirmed: a client comes from SevdeskClient.connect")

    async def _reply(self, path: str, params: Params | None) -> Reply:
        self._ensure_confirmed()
        return await self._http.read(path, params=params)

    async def _read(self, path: str, params: Params | None) -> object:
        return _objects(await self._reply(path, params), f"GET {path}")

    async def _get_one[M: SevdeskModel](self, model: type[M], path: str, params: Params | None = None) -> M:
        return _one(model, await self._read(path, params), f"GET {path}")

    async def _get_list[M: SevdeskModel](self, model: type[M], path: str, params: Params | None = None) -> list[M]:
        return _many(model, await self._read(path, params), f"GET {path}")

    async def _get_by_id[M: SevdeskModel](self, model: type[M], path: str) -> M | None:
        """The one object at ``path``, or ``None`` if sevDesk knows no such id (a 404 or an empty list); any other
        rejection, a 401 among them, is raised."""
        try:
            found = await self._get_list(model, path)
        except IntegrationRejectedError as exc:
            if exc.status == 404:
                return None
            raise
        return found[0] if found else None

    async def _get_all[M: SevdeskObject](self, model: type[M], path: str, params: Params | None = None) -> list[M]:
        """Every page of a listing, complete or refused: pages of ``PAGE_LIMIT`` until sevDesk's ``total`` is read.

        A page longer than the limit, an object seen twice, more than ``MAX_PAGES`` pages or a listing that ends
        short of its ``total`` is an ``UnexpectedResponseError``: an incomplete list must not pass for a whole one.
        Whether sevDesk reports ``total`` on every listing is a spike check.
        """
        where = f"GET {path}"
        items: list[M] = []
        seen: set[int] = set()
        for _ in range(MAX_PAGES):
            page_params = {**(params or {}), "limit": PAGE_LIMIT, "offset": len(items), "countAll": True}
            total, page = _page(model, await self._reply(path, page_params), where)
            if len(page) > PAGE_LIMIT:
                raise UnexpectedResponseError(f"sevdesk {where}: a page longer than {PAGE_LIMIT}")
            ids = {item.id for item in page}
            if len(ids) < len(page) or not ids.isdisjoint(seen):
                raise UnexpectedResponseError(f"sevdesk {where}: an object appears twice")
            seen |= ids
            items.extend(page)
            if len(page) < PAGE_LIMIT or len(items) >= total:
                if len(items) != total:
                    raise UnexpectedResponseError(f"sevdesk {where}: {len(items)} objects listed of {total}")
                return items
        raise UnexpectedResponseError(f"sevdesk {where}: more than {MAX_PAGES} pages")


def error_message(reply: Reply) -> str | None:
    """sevDesk's error text: ``{"error": {"message": ...}}``."""
    try:
        body = reply.json()
    except ValueError:
        return None
    match body:
        case {"error": {"message": str(message)}}:
            return message
    return None


def _objects(reply: Reply, where: str) -> object:
    try:
        body = reply.json()
    except ValueError:
        raise UnexpectedResponseError(f"sevdesk {where}: the response is not JSON") from None
    match body:
        case {"objects": objects}:
            return objects
    return body


class _Page(SevdeskModel):
    """One page of a ``countAll`` listing."""

    total: NonNegativeInt
    objects: list[object]


def _page[M: SevdeskModel](model: type[M], reply: Reply, where: str) -> tuple[int, list[M]]:
    try:
        body = reply.json()
    except ValueError:
        raise UnexpectedResponseError(f"sevdesk {where}: the response is not JSON") from None
    page = _validated(where, lambda: _Page.model_validate(body))
    return page.total, _many(model, page.objects, where)


def _one[M: SevdeskModel](model: type[M], data: object, where: str) -> M:
    return _validated(where, lambda: model.model_validate(unwrap_single(data)))


def _many[M: SevdeskModel](model: type[M], data: object, where: str) -> list[M]:
    """A list of objects; a single object counts as a list of one, as sevDesk answers either way."""
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise UnexpectedResponseError(f"sevdesk {where}: expected a list")
    return _validated(where, lambda: [model.model_validate(item) for item in data])


def _validated[T](where: str, validate: Callable[[], T]) -> T:
    """Validate a response; the error names the fields, never their values, which may be personal data."""
    try:
        return validate()
    except ValidationError as exc:
        fields = sorted({".".join(map(str, error["loc"])) for error in exc.errors()})
        raise UnexpectedResponseError(f"sevdesk {where}: unexpected response at {fields}") from None
