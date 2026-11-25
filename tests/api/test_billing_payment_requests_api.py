"""The payment-request lookup through its route (billing spec, SF-PAY), against a synthetic sevDesk.

The invoices are SP-SD's recorded objects (never edited); the filtered listing is the documented one
(``tests/fixtures/sevdesk/documented/``). The route reads sevDesk only - the identity, the invoice by its number and,
for a `200`, the contact - and touches no database: the session dependency fails the test if it is asked for.
"""

from collections.abc import AsyncGenerator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.api.v1.billing.params import get_billing_settings, get_sevdesk
from app.core.auth import AuthSettings, Scope, create_application_access_token
from app.core.auth.dependencies import get_auth_settings
from app.core.config import BillingSettings
from app.core.db.dependencies import get_db_session
from app.core.logging import LogFormat, LoggingSettings, LogLevel, configure_logging
from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.sevdesk.config import SevdeskSettings
from app.main import app
from app.services.billing.payment_requests import sevdesk_connect
from tests.integrations.sevdesk.fake_sevdesk import (
    ACCOUNT_ID,
    TOKEN,
    FakeSevdesk,
    invoice_listing,
    objects,
    recorded_object,
)

ROUTE = "/api/v1/billing/payment-requests/{}"
CONTACT_ID = 140992441
"""The contact of every recorded invoice: ``contact-reads-12``, customer number ``1000``."""
CUSTOMER_NUMBER = "1000"
IDENTITY = "GET /SevClient"
LISTING = "GET /Invoice"
CONTACT = f"GET /Contact/{CONTACT_ID}"
NOT_PAYABLE = {"detail": "No payable invoice has this number", "code": "invoice_not_payable"}
UNAVAILABLE = {"detail": "sevDesk could not be read", "code": "sevdesk_unavailable"}
REFUSED = {
    "detail": "SkillForge refused to use sevDesk: the configuration or the account could not be confirmed",
    "code": "sevdesk_refused",
}

# The recorded invoices of the answer table (billing spec, "Fixture manifest": SF-PAY records nothing new).
OPEN = recorded_object("read-back-158")  # RE-1000, status 200, 45 EUR, nothing paid
PARTLY_PAID = recorded_object("read-back-195")  # RE-1001, status 750, 45 EUR, 20 EUR paid
PAID = recorded_object("read-back-163")  # RE-1002, status 1000, 97.50 EUR paid
CANCELLED_ORIGINAL = recorded_object("read-back-168")  # RE-1003, status 1000, nothing paid (Q12)
CANCELLATION = recorded_object("read-back-190")  # RE-1004, type SR
# A draft carrying a number: the listing of the contact's invoices holds one (RE-1003, status 100), numbered before
# the cancelled original took RE-1003 at issue (Q3); read-back-28's draft has no number, so no filter lists it.
NUMBERED_DRAFT = recorded_object("deleted-read-187", 139163753)


@pytest.fixture
def fake() -> FakeSevdesk:
    fake = FakeSevdesk()
    fake.on("GET", f"/Contact/{CONTACT_ID}", objects([recorded_object("contact-reads-12")]))
    return fake


@pytest.fixture
def sevdesk_settings() -> SevdeskSettings:
    return SevdeskSettings(
        _env_file=None,
        mode=IntegrationMode.DRY_RUN,
        account=IntegrationAccount.TRIAL,
        token=SecretStr(TOKEN),
        expected_account_id=ACCOUNT_ID,
    )


@pytest.fixture
def restore_logging() -> Iterator[None]:
    yield
    configure_logging(LoggingSettings())


def _list(fake: FakeSevdesk, *invoices: Mapping[str, Any], under: str | None = None) -> None:
    """Let the filtered listing answer ``invoices`` for ``under`` - by default the number they carry."""
    number = under if under is not None else invoices[0]["invoiceNumber"]
    fake.on("GET", "/Invoice", invoice_listing({number: invoices}))


def _changed(invoice: Mapping[str, Any], **fields: object) -> dict[str, Any]:
    """A recorded invoice with fields changed in memory - for the cases the answer table does not name."""
    return {**invoice, **fields}


def _no_database() -> Iterator[object]:
    raise AssertionError("the payment-request lookup must not open a database session")
    yield  # pragma: no cover


@asynccontextmanager
async def _client(
    fake: FakeSevdesk, settings: SevdeskSettings, *, blocked: Sequence[str] = ()
) -> AsyncGenerator[AsyncClient]:
    app.dependency_overrides[get_db_session] = _no_database
    app.dependency_overrides[get_auth_settings] = _auth_settings
    app.dependency_overrides[get_sevdesk] = lambda: sevdesk_connect(settings, transport=fake.transport)
    app.dependency_overrides[get_billing_settings] = lambda: BillingSettings(
        _env_file=None, paypal_blocked_customer_numbers=",".join(blocked)
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def _look_up(
    fake: FakeSevdesk,
    settings: SevdeskSettings,
    number: str,
    *,
    blocked: Sequence[str] = (),
    scopes: Sequence[Scope] = (Scope.BILLING_PAYMENT_READ,),
) -> httpx.Response:
    async with _client(fake, settings, blocked=blocked) as client:
        return await client.get(ROUTE.format(number), headers=_auth_headers(*scopes))


# --- the answer table: one test per row ---


ANSWERS = {
    "open": (OPEN, {"invoice_number": "RE-1000", "state": "open", "open_amount_cents": 4500, "paypal": "allowed"}),
    "partly paid": (
        PARTLY_PAID,
        {"invoice_number": "RE-1001", "state": "open", "open_amount_cents": 2500, "paypal": "allowed"},
    ),
    "paid": (PAID, {"invoice_number": "RE-1002", "state": "paid", "open_amount_cents": 0, "paypal": "allowed"}),
}


@pytest.mark.parametrize(("invoice", "expected"), ANSWERS.values(), ids=ANSWERS.keys())
async def test_a_sent_invoice_answers_its_state_and_open_amount_with_four_fields_only(
    invoice: Mapping[str, Any], expected: dict[str, object], fake: FakeSevdesk, sevdesk_settings
):
    _list(fake, invoice)

    response = await _look_up(fake, sevdesk_settings, expected["invoice_number"])  # ty: ignore[invalid-argument-type]

    assert response.status_code == 200, response.text
    assert response.json() == expected
    assert type(response.json()["open_amount_cents"]) is int
    assert fake.sent() == [IDENTITY, LISTING, CONTACT]


def test_the_open_amount_is_the_gross_sum_minus_the_paid_amount():
    """The recorded partly paid invoice: 45 EUR gross, 20 EUR paid (a JSON number), 25 EUR open."""
    assert (PARTLY_PAID["sumGross"], PARTLY_PAID["paidAmount"], PARTLY_PAID["status"]) == ("45", 20, "750")


NOT_PAYABLE_CASES = {
    "an unknown number": (None, "RE-9999"),
    "a draft": (NUMBERED_DRAFT, "RE-1003"),
    "a cancellation (SR)": (CANCELLATION, "RE-1004"),
    "a cancelled original": (CANCELLED_ORIGINAL, "RE-1003"),
    "a dunning letter": (_changed(OPEN, invoiceType="MA"), "RE-1000"),
}


@pytest.mark.parametrize(("invoice", "number"), NOT_PAYABLE_CASES.values(), ids=NOT_PAYABLE_CASES.keys())
async def test_a_number_that_is_no_payable_invoice_is_404_and_says_not_which_case(
    invoice: Mapping[str, Any] | None, number: str, fake: FakeSevdesk, sevdesk_settings
):
    _list(fake, *([invoice] if invoice else []), under=number)

    response = await _look_up(fake, sevdesk_settings, number)

    assert (response.status_code, response.json()) == (404, NOT_PAYABLE)
    assert fake.sent() == [IDENTITY, LISTING]


async def test_a_draft_that_kept_an_issued_number_shadows_nothing(fake: FakeSevdesk, sevdesk_settings):
    """A draft can keep a number an issued invoice took (Q3): the issued one answers, the draft is set aside."""
    _list(fake, _changed(NUMBERED_DRAFT, invoiceNumber="RE-1000"), OPEN)

    response = await _look_up(fake, sevdesk_settings, "RE-1000")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "invoice_number": "RE-1000",
        "state": "open",
        "open_amount_cents": 4500,
        "paypal": "allowed",
    }
    assert fake.sent() == [IDENTITY, LISTING, CONTACT]


async def test_the_recorded_draft_beside_its_cancelled_original_answers_as_the_original(
    fake: FakeSevdesk, sevdesk_settings
):
    """``deleted-read-187`` lists the cancelled original and a draft under RE-1003: the original decides - 404."""
    _list(fake, CANCELLED_ORIGINAL, NUMBERED_DRAFT)

    response = await _look_up(fake, sevdesk_settings, "RE-1003")

    assert (response.status_code, response.json()) == (404, NOT_PAYABLE)


async def test_drafts_alone_under_a_number_are_404(fake: FakeSevdesk, sevdesk_settings):
    second_draft = _changed(NUMBERED_DRAFT, id="139163754")
    _list(fake, NUMBERED_DRAFT, second_draft)

    response = await _look_up(fake, sevdesk_settings, "RE-1003")

    assert (response.status_code, response.json()) == (404, NOT_PAYABLE)
    assert fake.sent() == [IDENTITY, LISTING]


async def test_the_filter_is_sent_as_documented_and_an_invoice_under_another_number_is_none_of_it(
    fake: FakeSevdesk, sevdesk_settings
):
    """Whether sevDesk filters exactly is Q19's; until then an invoice listed under another number never answers."""
    fake.on("GET", "/Invoice", invoice_listing({"RE-100": [OPEN]}))

    response = await _look_up(fake, sevdesk_settings, "RE-100")

    assert (response.status_code, response.json()) == (404, NOT_PAYABLE)
    [listing] = [request for request in fake.requests if request.url.path == "/api/v1/Invoice"]
    assert listing.url.params["invoiceNumber"] == "RE-100"


FAILED_READS = {
    "a 500": httpx.Response(500, json={"error": {"message": "synthetic"}}),
    "sevDesk's own 429": httpx.Response(429, json={"error": {"message": "synthetic"}}, headers={"Retry-After": "1"}),
    "a 401": httpx.Response(401, json={"status": 401, "message": "synthetic"}),
    "a lost connection": httpx.ConnectError("synthetic: connection refused"),
    "a body that is no listing": httpx.Response(200, json={"objects": "synthetic"}),
}


@pytest.mark.parametrize("failure", FAILED_READS.values(), ids=FAILED_READS.keys())
@pytest.mark.parametrize("read", ["/Invoice", f"/Contact/{CONTACT_ID}"])
async def test_a_read_that_fails_is_502_sevdesk_unavailable(
    read: str, failure: httpx.Response | Exception, fake: FakeSevdesk, sevdesk_settings
):
    _list(fake, OPEN)
    fake.on("GET", read, failure)

    response = await _look_up(fake, sevdesk_settings, "RE-1000")

    assert (response.status_code, response.json()) == (502, UNAVAILABLE)
    assert fake.sent("GET") == fake.sent()


REFUSALS = {
    "another account": lambda settings, fake: fake.on("GET", "/SevClient", objects([{"id": "4712", "name": "X"}])),
    "an identity read that fails": lambda settings, fake: fake.on("GET", "/SevClient", httpx.Response(429)),
    "no token": lambda settings, fake: settings.model_copy(update={"token": None}),
    "no expected account": lambda settings, fake: settings.model_copy(update={"expected_account_id": None}),
}


@pytest.mark.parametrize("refusal", REFUSALS.values(), ids=REFUSALS.keys())
async def test_a_refused_call_is_502_sevdesk_refused_and_reads_no_invoice(refusal, fake: FakeSevdesk, sevdesk_settings):
    _list(fake, OPEN)
    settings = refusal(sevdesk_settings, fake) or sevdesk_settings

    response = await _look_up(fake, settings, "RE-1000")

    assert (response.status_code, response.json()) == (502, REFUSED)
    assert set(fake.sent()) <= {IDENTITY}


UNNAMED = {
    "several invoices under one number that are no draft": (
        [CANCELLED_ORIGINAL, _changed(OPEN, invoiceNumber="RE-1003"), NUMBERED_DRAFT],
        "RE-1003",
    ),
    "another status": ([_changed(OPEN, status="300")], "RE-1000"),
    "an open amount below 0": ([_changed(OPEN, paidAmount=50)], "RE-1000"),
    "a paid amount below 0": ([_changed(PAID, paidAmount=-1)], "RE-1002"),
    "a gross sum that does not parse": ([_changed(OPEN, sumGross="45.001")], "RE-1000"),
    "a paid amount that does not parse": ([_changed(OPEN, paidAmount="twenty")], "RE-1000"),
    "no gross sum": ([_changed(OPEN, sumGross=None)], "RE-1000"),
    "no paid amount": ([_changed(PARTLY_PAID, paidAmount=None)], "RE-1001"),
    "no type": ([_changed(OPEN, invoiceType=None)], "RE-1000"),
}


@pytest.mark.parametrize(("invoices", "number"), UNNAMED.values(), ids=UNNAMED.keys())
async def test_anything_the_table_does_not_name_is_502_sevdesk_unavailable(
    invoices: list[Mapping[str, Any]], number: str, fake: FakeSevdesk, sevdesk_settings
):
    _list(fake, *invoices, under=number)

    response = await _look_up(fake, sevdesk_settings, number)

    assert (response.status_code, response.json()) == (502, UNAVAILABLE)
    assert fake.sent() == [IDENTITY, LISTING]


async def test_an_invoice_whose_contact_sevdesk_does_not_know_is_502_sevdesk_unavailable(
    fake: FakeSevdesk, sevdesk_settings
):
    _list(fake, OPEN)
    fake.on("GET", f"/Contact/{CONTACT_ID}", objects([]))

    response = await _look_up(fake, sevdesk_settings, "RE-1000")

    assert (response.status_code, response.json()) == (502, UNAVAILABLE)


# --- the request ---


@pytest.mark.parametrize(
    "number",
    ["re-1000", "-RE-1000", ".RE", "_RE", "RE 1000", "RE+1000", "RE%1000", "RÉ-1000", "R" * 33, "RE-1000\n"],
)
async def test_a_number_off_the_pattern_is_422_and_reaches_no_sevdesk(number: str, fake: FakeSevdesk, sevdesk_settings):
    _list(fake, OPEN)

    async with _client(fake, sevdesk_settings) as client:
        response = await client.get(
            ROUTE.format(quote(number, safe="")), headers=_auth_headers(Scope.BILLING_PAYMENT_READ)
        )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"
    assert fake.requests == []


@pytest.mark.parametrize("path", ["RE/1000", "RE%2F1000", ""])
async def test_a_number_with_a_slash_or_none_reaches_no_sevdesk(path: str, fake: FakeSevdesk, sevdesk_settings):
    async with _client(fake, sevdesk_settings) as client:
        response = await client.get(ROUTE.format(path), headers=_auth_headers(Scope.BILLING_PAYMENT_READ))

    assert response.status_code in {404, 405, 422}
    assert fake.requests == []


@pytest.mark.parametrize("number", ["R", "0", "RE-1000", "RE.2026_11-1", "A" + "9" * 31])
async def test_a_number_on_the_pattern_is_looked_up(number: str, fake: FakeSevdesk, sevdesk_settings):
    _list(fake, _changed(OPEN, invoiceNumber=number))

    response = await _look_up(fake, sevdesk_settings, number)

    assert response.status_code == 200, response.text
    assert response.json()["invoice_number"] == number


@pytest.mark.parametrize(
    "scopes",
    [(Scope.BILLING_READ,), (Scope.BILLING_WRITE,), (Scope.BILLING_READ, Scope.BILLING_WRITE, Scope.CRM_READ)],
)
async def test_the_billing_scopes_do_not_stand_in_for_the_payment_scope(
    scopes: Sequence[Scope], fake: FakeSevdesk, sevdesk_settings
):
    _list(fake, OPEN)

    response = await _look_up(fake, sevdesk_settings, "RE-1000", scopes=scopes)

    assert response.status_code == 403
    assert fake.requests == []


# --- PayPal ---


PAYPAL = {
    "blocked by its customer number": ([CUSTOMER_NUMBER], "blocked"),
    "blocked among others": (["999", CUSTOMER_NUMBER, "KD-1"], "blocked"),
    "allowed with the setting empty": ([], "allowed"),
    "allowed by another number": (["100", "10000", "01000", "KD-1000"], "allowed"),
}


@pytest.mark.parametrize(("blocked", "paypal"), PAYPAL.values(), ids=PAYPAL.keys())
async def test_paypal_is_blocked_exactly_for_a_customer_number_in_the_setting(
    blocked: list[str], paypal: str, fake: FakeSevdesk, sevdesk_settings
):
    _list(fake, OPEN)

    response = await _look_up(fake, sevdesk_settings, "RE-1000", blocked=blocked)

    assert response.status_code == 200, response.text
    assert response.json()["paypal"] == paypal


async def test_paypal_is_blocked_for_a_paid_invoice_too(fake: FakeSevdesk, sevdesk_settings):
    _list(fake, PAID)

    response = await _look_up(fake, sevdesk_settings, "RE-1002", blocked=[CUSTOMER_NUMBER])

    assert response.json() == {
        "invoice_number": "RE-1002",
        "state": "paid",
        "open_amount_cents": 0,
        "paypal": "blocked",
    }


async def test_a_contact_without_a_customer_number_is_allowed(fake: FakeSevdesk, sevdesk_settings):
    _list(fake, OPEN)
    contact = {**recorded_object("contact-reads-12"), "customerNumber": None}
    fake.on("GET", f"/Contact/{CONTACT_ID}", objects([contact]))

    response = await _look_up(fake, sevdesk_settings, "RE-1000", blocked=[CUSTOMER_NUMBER, ""])

    assert response.json()["paypal"] == "allowed"


# --- what leaves the service ---


async def test_no_response_log_line_or_error_holds_a_name_a_contact_id_or_a_customer_number(
    fake: FakeSevdesk, sevdesk_settings, restore_logging, capsys, caplog
):
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()
    # A synthetic person on the recorded contact, and a customer number no other value in the output could contain.
    customer_number = "KD-73915864"
    contact = {
        **recorded_object("contact-reads-12"),
        "customerNumber": customer_number,
        "surename": "Synthia",
        "familyname": "Examplova",
        "name": "Example Family Company",
    }
    fake.on("GET", f"/Contact/{CONTACT_ID}", objects([contact]))
    invoice = _changed(PARTLY_PAID, addressName="Synthia Examplova")
    responses = []
    for answer in (invoice, _changed(invoice, status="300"), _changed(invoice, invoiceType="SR")):
        _list(fake, answer)
        responses.append(await _look_up(fake, sevdesk_settings, "RE-1001", blocked=[customer_number]))
    fake.on("GET", f"/Contact/{CONTACT_ID}", httpx.Response(500, json={"error": {"message": "Synthia Examplova"}}))
    _list(fake, invoice)
    responses.append(await _look_up(fake, sevdesk_settings, "RE-1001", blocked=[customer_number]))

    output = capsys.readouterr().out
    assert [response.status_code for response in responses] == [200, 502, 404, 502]
    assert responses[0].json()["paypal"] == "blocked"
    assert "payment_request" in output
    for text in (output, caplog.text, *(response.text for response in responses)):
        for value in (str(CONTACT_ID), customer_number, "Synthia", "Examplova", "Example Family"):
            assert value not in text


def _auth_headers(*scopes: Scope) -> dict[str, str]:
    token = create_application_access_token(
        _auth_settings(),
        principal_id=UUID("00000000-0000-0000-0000-000000000001"),
        client_id="skillsite",
        scopes=[str(scope) for scope in scopes],
    )
    return {"Authorization": f"Bearer {token.access_token}"}


def _auth_settings() -> AuthSettings:
    return AuthSettings(secret_key=SecretStr("test-signing-secret-with-at-least-32-bytes"))
