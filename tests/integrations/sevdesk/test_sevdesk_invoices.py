"""Invoice drafts: the saveInvoice body under § 19 UStG in exact decimal strings, the save and the reconcile reads."""

import json
import traceback
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.integrations.errors import (
    IdentityCheckError,
    IntegrationRefusedError,
    OutcomeUnknownError,
    UnexpectedResponseError,
)
from app.integrations.sevdesk import client as sevdesk_client
from app.integrations.sevdesk.client import SevdeskClient
from app.integrations.sevdesk.invoices import InvoiceDraft, PositionDraft, euros, hours, invoice_payload
from app.integrations.sevdesk.models import Invoice, InvoicePosition
from app.integrations.transport import PlannedRequest
from tests.integrations.sevdesk.fake_sevdesk import ACCOUNT_ID, TOKEN, FakeSevdesk, objects, page

POSITION_DRAFT = PositionDraft(name="Maths", minutes=90, price_cents=3000, unity_id=12, part_id=31)
DRAFT = InvoiceDraft(
    contact_id=21,
    invoice_date=date(2026, 11, 2),
    delivery_date=date(2026, 10, 1),
    delivery_date_until=date(2026, 10, 31),
    header="Invoice 2026-10",
    tax_text="Synthetic tax text",
    positions=(POSITION_DRAFT,),
    customer_internal_note="skillforge:run-0001",
    foot_text="Synthetic foot text",
)


def test_the_body_is_a_draft_under_section_19():
    body = invoice_payload(DRAFT, contact_person_id=11)

    assert list(body) == [
        "invoice",
        "invoicePosSave",
        "invoicePosDelete",
        "discountSave",
        "discountDelete",
        "takeDefaultAddress",
    ]
    assert body["invoice"] == {
        "objectName": "Invoice",
        "mapAll": True,
        "invoiceDate": "2026-11-02",
        "deliveryDate": "2026-10-01",
        "deliveryDateUntil": "2026-10-31",
        "contact": {"id": 21, "objectName": "Contact"},
        "contactPerson": {"id": 11, "objectName": "SevUser"},
        "status": 100,
        "invoiceType": "RE",
        "currency": "EUR",
        "taxRule": {"id": 11, "objectName": "TaxRule"},
        "taxRate": 0,
        "discount": 0,
        "taxText": "Synthetic tax text",
        "header": "Invoice 2026-10",
        "footText": "Synthetic foot text",
        "customerInternalNote": "skillforge:run-0001",
    }
    invoice = body["invoice"]
    assert isinstance(invoice, dict)
    assert [type(invoice[key]) for key in ("taxRate", "discount")] == [int, int], "0, never False"
    assert body["invoicePosSave"] == [
        {
            "objectName": "InvoicePos",
            "mapAll": True,
            "name": "Maths",
            "quantity": "1.5",
            "price": "30.00",
            "taxRate": "0",
            "unity": {"id": 12, "objectName": "Unity"},
            "part": {"id": 31, "objectName": "Part"},
        }
    ]
    assert (body["invoicePosDelete"], body["discountSave"], body["discountDelete"]) == (None, None, None)
    assert body["takeDefaultAddress"] is True


def test_a_single_delivery_day_sends_no_until():
    draft = InvoiceDraft(
        contact_id=21,
        invoice_date=date(2026, 11, 2),
        delivery_date=date(2026, 10, 30),
        header="Invoice",
        tax_text="Synthetic tax text",
        positions=(POSITION_DRAFT,),
    )

    invoice = invoice_payload(draft, contact_person_id=11)["invoice"]

    assert isinstance(invoice, dict)
    assert invoice["deliveryDate"] == "2026-10-30" and "deliveryDateUntil" not in invoice


def test_a_delivery_period_ending_before_it_starts_is_refused():
    with pytest.raises(ValueError, match="period"):
        InvoiceDraft(
            contact_id=21,
            invoice_date=date(2026, 11, 2),
            delivery_date=date(2026, 10, 31),
            delivery_date_until=date(2026, 10, 1),
            header="Invoice",
            tax_text="Synthetic tax text",
            positions=(POSITION_DRAFT,),
        )


def _floats(value: object) -> list[float]:
    match value:
        case float():
            return [value]
        case dict():
            return [found for item in value.values() for found in _floats(item)]
        case list() | tuple():
            return [found for item in value for found in _floats(item)]
    return []


def test_no_float_appears_in_the_body():
    positions = tuple(
        PositionDraft(name="Position", minutes=minutes, price_cents=cents, unity_id=12)
        for minutes, cents in ((45, 3000), (60, 4), (135, 0), (600, 123456788))
    )
    body = invoice_payload(
        InvoiceDraft(
            contact_id=21,
            invoice_date=date(2026, 11, 2),
            delivery_date=date(2026, 10, 1),
            header="Invoice",
            tax_text="Synthetic tax text",
            positions=positions,
            time_to_pay=14,
        ),
        contact_person_id=11,
    )

    assert _floats(body) == []
    sent = body["invoicePosSave"]
    assert isinstance(sent, list)
    assert [(position["quantity"], position["price"]) for position in sent] == [
        ("0.75", "30.00"),
        ("1", "0.04"),
        ("2.25", "0.00"),
        ("10", "1234567.88"),
    ]


@pytest.mark.parametrize(("minutes", "expected"), [(15, "0.25"), (45, "0.75"), (60, "1"), (90, "1.5"), (600, "10")])
def test_minutes_become_exact_hours(minutes: int, expected: str):
    assert f"{hours(minutes):f}" == expected


@pytest.mark.parametrize("minutes", [50, 1, 100])
def test_minutes_without_an_exact_decimal_hour_are_refused_by_hours(minutes: int):
    with pytest.raises(ValueError, match="exact"):
        hours(minutes)


@pytest.mark.parametrize("minutes", [1, 3, 20, 46, 50, 100])
def test_minutes_off_the_quarter_hour_are_refused(minutes: int):
    with pytest.raises(ValueError, match="steps of 15"):
        PositionDraft(name="Position", minutes=minutes, price_cents=3000, unity_id=12)


@pytest.mark.parametrize("cents", [1, 2, 2999, 3001, 3002])
def test_a_price_per_hour_not_divisible_by_4_is_refused(cents: int):
    with pytest.raises(ValueError, match="divisible by 4"):
        PositionDraft(name="Position", minutes=60, price_cents=cents, unity_id=12)


@pytest.mark.parametrize(
    "change",
    [
        {"minutes": 60.0},
        {"minutes": True},
        {"price_cents": 3000.0},
        {"price_cents": Decimal(3000)},
        {"price_cents": "3000"},
        {"unity_id": "12"},
        {"part_id": 31.0},
    ],
)
def test_a_position_takes_ints_only(change: dict[str, Any]):
    arguments: dict[str, Any] = {"name": "Position", "minutes": 60, "price_cents": 3000, "unity_id": 12, **change}

    with pytest.raises(TypeError, match="must be an int"):
        PositionDraft(**arguments)


@pytest.mark.parametrize("minutes", range(15, 601, 15))
@pytest.mark.parametrize("cents", [0, 4, 2996, 3000, 4500, 6000, 99996])
def test_every_quarter_hour_position_is_whole_cents(minutes: int, cents: int):
    position = PositionDraft(name="Position", minutes=minutes, price_cents=cents, unity_id=12)

    assert hours(position.minutes) * position.price_cents == minutes * cents // 60


@pytest.mark.parametrize(("minutes", "cents"), [(0, 3000), (-60, 3000), (60, -1)])
def test_a_position_without_time_or_with_a_negative_price_is_refused(minutes: int, cents: int):
    with pytest.raises(ValueError, match="positive"):
        PositionDraft(name="Position", minutes=minutes, price_cents=cents, unity_id=12)


@pytest.mark.parametrize(
    ("cents", "expected"),
    [
        (3000, "30.00"),
        (5, "0.05"),
        (0, "0.00"),
        (-150, "-1.50"),
        (-5, "-0.05"),
        (123456789, "1234567.89"),
        (9007199254740993, "90071992547409.93"),
        (10**30 + 7, "10000000000000000000000000000.07"),
    ],
)
def test_cents_become_an_exact_decimal_string(cents: int, expected: str):
    assert euros(cents) == expected


@pytest.mark.parametrize("cents", [30.0, Decimal(3000), True])
def test_euros_takes_int_cents_only(cents: object):
    with pytest.raises(TypeError):
        euros(cents)  # ty: ignore[invalid-argument-type]


# --- reading invoices: lenient parsing, strict types, whole cents ---

INVOICE = {
    "id": "501",
    "objectName": "Invoice",
    "invoiceNumber": None,
    "status": "100",
    "contact": {"id": "21", "objectName": "Contact"},
    "invoiceDate": "2026-11-02T00:00:00+01:00",
    "taxRule": {"id": "11", "objectName": "TaxRule"},
    "smallSettlement": "1",
    "sumNet": "45.00",
    "sumGross": "45.00",
    "customerInternalNote": "skillforge:run-0001",
    "sevClient": {"id": str(ACCOUNT_ID), "objectName": "SevClient"},
}
POSITION = {
    "id": "601",
    "invoice": {"id": "501", "objectName": "Invoice"},
    "part": {"id": "31", "objectName": "Part"},
    "unity": {"id": "12", "objectName": "Unity"},
    "quantity": "1.5",
    "price": "30",
    "taxRate": "0",
    "positionNumber": "0",
    "name": "Maths",
}


def test_string_numbers_and_booleans_parse_into_strict_types():
    invoice = Invoice.model_validate(INVOICE)
    position = InvoicePosition.model_validate(POSITION)

    assert (invoice.id, invoice.status, invoice.contact.id, invoice.small_settlement) == (501, 100, 21, True)
    assert (invoice.sum_net_cents, invoice.sum_gross_cents) == (4500, 4500)
    assert invoice.tax_rule is not None and invoice.tax_rule.id == 11
    assert (position.quantity, position.price_cents, position.position_number) == (Decimal("1.5"), 3000, 0)
    assert Invoice.model_validate({**INVOICE, "smallSettlement": "0"}).small_settlement is False


@pytest.mark.parametrize("bad", [{"status": "draft"}, {"contact": None}, {"id": "five"}])
def test_a_wrong_type_is_rejected(bad):
    with pytest.raises(ValueError):
        Invoice.model_validate({**INVOICE, **bad})


@pytest.mark.parametrize("amount", ["45.00", "45", "45.0000", 45, Decimal("45.00")])
def test_an_amount_in_whole_cents_is_read_to_the_cent(amount: object):
    invoice = Invoice.model_validate({**INVOICE, "sumGross": amount})

    assert invoice.sum_gross_cents == 4500 and type(invoice.sum_gross_cents) is int


@pytest.mark.parametrize(("amount", "message"), [("45.005", "below a cent"), ("0.001", "below a cent"), (0.3, "float")])
def test_an_amount_below_a_cent_or_a_float_is_refused(amount: object, message: str):
    with pytest.raises(ValueError, match=message):
        Invoice.model_validate({**INVOICE, "sumNet": amount})
    with pytest.raises(ValueError, match=message):
        InvoicePosition.model_validate({**POSITION, "price": amount})


# --- reads the reconcile needs ---


async def test_an_invoice_is_read_by_id(settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/Invoice/501", objects(INVOICE))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        invoice = await client.get_invoice(501)

    assert invoice is not None and invoice.id == 501


async def test_an_invoice_read_by_id_must_be_the_one_asked_for(settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/Invoice/501", objects({**INVOICE, "id": "502"}))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        with pytest.raises(UnexpectedResponseError, match=r"GET /Invoice/\{id\}: the answer is not the object"):
            await client.get_invoice(501)


async def test_find_invoices_pages_and_filters_by_the_note_itself(settings, fake_sevdesk: FakeSevdesk, monkeypatch):
    monkeypatch.setattr(sevdesk_client, "PAGE_LIMIT", 2)
    other = {**INVOICE, "id": "502", "customerInternalNote": "skillforge:run-0002"}
    pages = {"0": [INVOICE, other], "2": [{**INVOICE, "id": "503"}]}
    fake_sevdesk.on("GET", "/Invoice", lambda request: page(pages[request.url.params["offset"]], total=3))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        found = await client.find_invoices("skillforge:run-0001")

    assert [invoice.id for invoice in found] == [501, 503]
    params = [dict(request.url.params) for request in fake_sevdesk.requests[1:]]
    assert params == [
        {"customerInternalNote": "skillforge:run-0001", "limit": "2", "offset": "0", "countAll": "true"},
        {"customerInternalNote": "skillforge:run-0001", "limit": "2", "offset": "2", "countAll": "true"},
    ]


async def test_the_positions_of_an_invoice(settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/Invoice/501/getPositions", page([POSITION]))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        positions = await client.invoice_positions(501)

    assert [(position.id, position.quantity, position.price_cents) for position in positions] == [
        (601, Decimal("1.5"), 3000)
    ]


async def test_the_invoice_numbering(settings, fake_sevdesk: FakeSevdesk):
    sequence = {"id": "5", "format": "RE-[%NUMBER%]", "nextSequence": "1001", "type": "RE"}
    fake_sevdesk.on("GET", "/SevSequence/Factory/getByType", objects(sequence))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        numbering = await client.invoice_sequence()

    assert (numbering.id, numbering.format, numbering.next_sequence) == (5, "RE-[%NUMBER%]", 1001)
    assert dict(fake_sevdesk.requests[-1].url.params) == {"objectType": "Invoice", "type": "RE"}


# --- the saveInvoice write ---


async def test_a_dry_run_save_returns_the_planned_request_and_sends_no_post(settings, fake_sevdesk: FakeSevdesk):
    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        planned = await client.save_invoice(DRAFT)

    assert isinstance(planned, PlannedRequest)
    assert (planned.method, planned.url) == ("POST", "https://my.sevdesk.de/api/v1/Invoice/Factory/saveInvoice")
    assert fake_sevdesk.sent("POST") == []
    assert fake_sevdesk.sent() == ["GET /SevClient"]


async def test_a_save_without_contact_person_is_refused(settings, fake_sevdesk: FakeSevdesk):
    async with SevdeskClient.connect(
        settings.model_copy(update={"contact_person_id": None}), transport=fake_sevdesk.transport
    ) as client:
        with pytest.raises(IntegrationRefusedError, match="CONTACT_PERSON_ID"):
            await client.save_invoice(DRAFT)


async def test_a_live_save_returns_the_saved_draft(live_settings, fake_sevdesk: FakeSevdesk):
    saved = objects({"invoice": INVOICE, "invoicePos": [POSITION]}, status=201)
    fake_sevdesk.on("POST", "/Invoice/Factory/saveInvoice", saved)

    async with SevdeskClient.connect(live_settings, transport=fake_sevdesk.transport) as client:
        result = await client.save_invoice(DRAFT)

    assert not isinstance(result, PlannedRequest)
    assert (result.invoice.id, result.invoice.status, [p.id for p in result.positions]) == (501, 100, [601])
    sent = json.loads(fake_sevdesk.requests[-1].content)
    assert sent["invoice"]["customerInternalNote"] == "skillforge:run-0001"
    assert (sent["invoicePosSave"][0]["quantity"], sent["invoicePosSave"][0]["price"]) == ("1.5", "30.00")


SAVE_FAILURES = {
    "a timeout": httpx.ReadTimeout("timed out"),
    "a dropped connection": httpx.RemoteProtocolError("closed"),
    "a refused connection": httpx.ConnectError("refused"),
    "a 500 validation error": httpx.Response(500, json={"error": {"message": "Synthetic validation error"}}),
    "an unreadable success": httpx.Response(201, json={"objects": {"invoice": {"id": "501"}}}),
    "a success that is no JSON": httpx.Response(201, content=b"<html>saved</html>"),
}


@pytest.mark.parametrize("answer", SAVE_FAILURES.values(), ids=SAVE_FAILURES.keys())
async def test_a_failed_save_is_sent_once_and_its_outcome_unknown(answer, live_settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("POST", "/Invoice/Factory/saveInvoice", answer)

    async with SevdeskClient.connect(live_settings, transport=fake_sevdesk.transport) as client:
        with pytest.raises(OutcomeUnknownError):
            await client.save_invoice(DRAFT)

    assert fake_sevdesk.sent("POST") == ["POST /Invoice/Factory/saveInvoice"]


async def test_a_success_nested_too_deep_to_parse_has_an_unknown_outcome(
    live_settings, fake_sevdesk: FakeSevdesk, too_deep: bytes
):
    fake_sevdesk.on("POST", "/Invoice/Factory/saveInvoice", httpx.Response(201, content=too_deep))

    async with SevdeskClient.connect(live_settings, transport=fake_sevdesk.transport) as client:
        with pytest.raises(OutcomeUnknownError, match="the response is not JSON; the write went through"):
            await client.save_invoice(DRAFT)

    assert fake_sevdesk.sent("POST") == ["POST /Invoice/Factory/saveInvoice"]


async def test_an_unconfirmed_client_saves_nothing(settings, fake_sevdesk: FakeSevdesk):
    async with sevdesk_client._sevdesk_http(settings, transport=fake_sevdesk.transport) as http:
        with pytest.raises(IdentityCheckError, match="not confirmed"):
            await SevdeskClient(http, settings).save_invoice(DRAFT)

    assert fake_sevdesk.requests == []


async def test_a_failed_save_leaks_no_token_into_its_traceback(live_settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("POST", "/Invoice/Factory/saveInvoice", httpx.LocalProtocolError(f"Illegal header {TOKEN}"))

    async with SevdeskClient.connect(live_settings, transport=fake_sevdesk.transport) as client:
        with pytest.raises(OutcomeUnknownError) as raised:
            await client.save_invoice(DRAFT)

    assert TOKEN not in "".join(traceback.format_exception(raised.value))
