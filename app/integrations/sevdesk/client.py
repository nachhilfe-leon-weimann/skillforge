"""The sevDesk client: sevDesk's API as SkillForge uses it, nothing more.

sevDesk wraps a result in ``{"objects": ...}``, types numbers as strings, answers some validation errors with a
500 and ``{"error": {"message": ...}}``, and pages with ``limit`` (1 to 1000), ``offset`` and ``countAll``. A
client comes from ``SevdeskClient.connect``, which confirms the account; until then a client sends nothing but
the identity read.
"""

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Self

from pydantic import NonNegativeInt, ValidationError

from app.integrations.errors import (
    IdentityCheckError,
    IntegrationRefusedError,
    IntegrationRejectedError,
    OutcomeUnknownError,
    UnexpectedResponseError,
)
from app.integrations.transport import (
    InnerTransport,
    IntegrationHttp,
    Observer,
    Params,
    PlannedRequest,
    Reply,
    path_template,
)

from .config import SevdeskSettings
from .identity import TRIAL_MARKER, verify_account
from .invoices import InvoiceDraft, invoice_payload
from .models import (
    Account,
    BookkeepingVersion,
    Contact,
    ContactCustomField,
    CustomFieldSetting,
    Invoice,
    InvoicePosition,
    InvoiceSequence,
    Part,
    SavedInvoice,
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

    @classmethod
    async def discover(cls, settings: SevdeskSettings, *, transport: InnerTransport | None = None) -> Discovery:
        """The account the token belongs to, read without the identity check, which needs its id: how the
        maintainer finds ``SEVDESK__EXPECTED_ACCOUNT_ID``. The one path that reads before the account is
        confirmed; it sends exactly the three reads of spike row S2 and nothing else."""
        async with _sevdesk_http(settings, transport=transport) as http:
            account = await cls(http, settings).account()
            users = _many(SevUser, _objects(await http.read("/SevUser"), "GET /SevUser"), "GET /SevUser")
            where = "GET /ContactCustomFieldSetting"
            field_settings = _many(
                CustomFieldSetting, _objects(await http.read("/ContactCustomFieldSetting"), where), where
            )
        return Discovery(
            account_id=account.id,
            trial_marker=TRIAL_MARKER in account.name,
            user_account_ids=_account_ids(users),
            field_setting_account_ids=_account_ids(field_settings),
        )

    @property
    def http(self) -> IntegrationHttp:
        """The guarded HTTP client of the confirmed account, for the calls this client does not model (the spike's)."""
        self._ensure_confirmed()
        return self._http

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
        return await self._get_by_id(SevUser, "/SevUser", user_id)

    async def get_unity(self, unity_id: int) -> Unity | None:
        return await self._get_by_id(Unity, "/Unity", unity_id)

    async def get_contact(self, contact_id: int) -> Contact | None:
        return await self._get_by_id(Contact, "/Contact", contact_id)

    async def custom_field_settings(self) -> list[CustomFieldSetting]:
        return await self._get_list(CustomFieldSetting, "/ContactCustomFieldSetting")

    async def get_custom_field_setting(self, setting_id: int) -> CustomFieldSetting | None:
        return await self._get_by_id(CustomFieldSetting, "/ContactCustomFieldSetting", setting_id)

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

    async def invoice_sequence(self) -> InvoiceSequence:
        params = {"objectType": "Invoice", "type": "RE"}
        return await self._get_one(InvoiceSequence, "/SevSequence/Factory/getByType", params)

    async def get_invoice(self, invoice_id: int) -> Invoice | None:
        return await self._get_by_id(Invoice, "/Invoice", invoice_id)

    async def invoice_positions(self, invoice_id: int) -> list[InvoicePosition]:
        return await self._get_all(InvoicePosition, f"/Invoice/{invoice_id}/getPositions")

    async def find_invoices(self, customer_internal_note: str) -> list[Invoice]:
        """The invoices whose ``customerInternalNote`` equals the given one: the reconcile's lookup.

        Whether sevDesk filters by that field is a spike question, so the result is filtered here as well.
        """
        params = {"customerInternalNote": customer_internal_note}
        invoices = await self._get_all(Invoice, "/Invoice", params)
        return [invoice for invoice in invoices if invoice.customer_internal_note == customer_internal_note]

    # --- writes: sent only in live mode, never retried ---

    async def save_invoice(self, draft: InvoiceDraft) -> SavedInvoice | PlannedRequest:
        """Create a draft invoice with its positions (System 2.0 creates drafts only)."""
        self._ensure_confirmed()
        if self._settings.contact_person_id is None:
            raise IntegrationRefusedError("SEVDESK__CONTACT_PERSON_ID is not set")
        body = invoice_payload(draft, contact_person_id=self._settings.contact_person_id)
        written = await self._http.write("POST", "/Invoice/Factory/saveInvoice", json=body)
        return _written(SavedInvoice, written, "POST /Invoice/Factory/saveInvoice")

    # --- plumbing ---

    def _ensure_confirmed(self) -> None:
        if not self._confirmed:
            raise IdentityCheckError("the sevDesk account is not confirmed: a client comes from SevdeskClient.connect")

    async def _reply(self, path: str, params: Params | None) -> Reply:
        self._ensure_confirmed()
        return await self._http.read(path, params=params)

    async def _read(self, path: str, params: Params | None) -> object:
        return _objects(await self._reply(path, params), _get(path))

    async def _get_one[M: SevdeskModel](self, model: type[M], path: str, params: Params | None = None) -> M:
        return _one(model, await self._read(path, params), _get(path))

    async def _get_list[M: SevdeskModel](self, model: type[M], path: str, params: Params | None = None) -> list[M]:
        return _many(model, await self._read(path, params), _get(path))

    async def _get_by_id[M: SevdeskObject](self, model: type[M], collection: str, object_id: int) -> M | None:
        """The object with ``object_id``, or ``None`` if sevDesk knows no such id (a 404 or an empty list); any
        other rejection, a 401 among them, is raised. An answer with more than one object, or another one, is an
        ``UnexpectedResponseError``: it must not pass for the object asked for."""
        path = f"{collection}/{object_id}"
        try:
            found = await self._get_list(model, path)
        except IntegrationRejectedError as exc:
            if exc.status == 404:
                return None
            raise
        if len(found) > 1 or any(item.id != object_id for item in found):
            raise UnexpectedResponseError(f"sevdesk {_get(path)}: the answer is not the object asked for")
        return found[0] if found else None

    async def _get_all[M: SevdeskObject](self, model: type[M], path: str, params: Params | None = None) -> list[M]:
        """Every page of a listing, complete or refused: pages of ``PAGE_LIMIT`` until sevDesk's ``total`` is read.

        A page longer than the limit, an object seen twice, more than ``MAX_PAGES`` pages or a listing that ends
        short of its ``total`` is an ``UnexpectedResponseError``: an incomplete list must not pass for a whole one.
        Whether sevDesk reports ``total`` on every listing is a spike check.
        """
        where = _get(path)
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


@dataclass(frozen=True, slots=True)
class Discovery:
    """What ``discover`` learned: account ids and whether the company name carries the trial marker - never the
    name itself."""

    account_id: int
    """The ``SevClient`` id: the candidate for ``SEVDESK__EXPECTED_ACCOUNT_ID``."""
    trial_marker: bool
    user_account_ids: tuple[int, ...]
    """The distinct ``sevClient`` ids of the ``SevUser`` objects, for comparison (spike question Q7)."""
    field_setting_account_ids: tuple[int, ...]
    """The distinct ``sevClient`` ids of the ``ContactCustomFieldSetting`` objects, for comparison."""


def _account_ids(objects: list[SevUser] | list[CustomFieldSetting]) -> tuple[int, ...]:
    return tuple(sorted({item.sev_client.id for item in objects if item.sev_client is not None}))


def _get(path: str) -> str:
    """A read as an error message names it: the path's template, never an id (``GET /Contact/{id}``)."""
    return f"GET {path_template(path)}"


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
    except ValueError, RecursionError:  # a body nested too deep for the parser
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
    except ValueError, RecursionError:  # a body nested too deep for the parser
        raise UnexpectedResponseError(f"sevdesk {where}: the response is not JSON") from None
    if not isinstance(body, dict) or "total" not in body:
        raise UnexpectedResponseError(f"sevdesk {where}: listing without countAll total")
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


def _written[M: SevdeskModel](model: type[M], written: Reply | PlannedRequest, where: str) -> M | PlannedRequest:
    """A write's result; a write that went through but whose answer cannot be read - for whatever reason, a body
    nested too deep for the parser included - has an unknown outcome."""
    if isinstance(written, PlannedRequest):
        return written
    try:
        return _one(model, _objects(written, where), where)
    except UnexpectedResponseError as exc:
        raise OutcomeUnknownError(f"{exc}; the write went through, its result is unknown") from None
    except Exception as exc:
        raise OutcomeUnknownError(
            f"sevdesk {where}: the answer failed ({type(exc).__name__}); the write went through, its result is unknown"
        ) from None
