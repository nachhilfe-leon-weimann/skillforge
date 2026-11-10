"""The sevDesk client against a synthetic sevDesk: wire format, lenient parsing, dry_run, no retry, no leaks."""

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.core.logging import LogFormat, LoggingSettings, LogLevel, configure_logging
from app.integrations.errors import (
    IntegrationRefusedError,
    IntegrationUnavailableError,
    OutcomeUnknownError,
    UnexpectedResponseError,
)
from app.integrations.sevdesk import client as sevdesk_client
from app.integrations.sevdesk.client import SevdeskClient
from app.integrations.sevdesk.invoices import InvoiceDraft, PositionDraft
from app.integrations.sevdesk.models import ContactCustomField, Invoice, InvoicePosition, Part
from app.integrations.transport import PlannedRequest
from tests.integrations.sevdesk.fake_sevdesk import ACCOUNT_ID, TOKEN, FakeSevdesk, objects

DRAFT = InvoiceDraft(
    contact_id=21,
    invoice_date=date(2026, 11, 2),
    header="Invoice 2026-10",
    tax_text="Synthetic tax text",
    positions=(PositionDraft(name="Maths", quantity=Decimal("1.5"), price=Decimal("30.00"), unity_id=12, part_id=31),),
    customer_internal_note="skillforge:run-0001",
    foot_text="Synthetic foot text",
)
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


# --- lenient parsing, strict types ---


def test_string_numbers_and_booleans_parse_into_strict_types():
    invoice = Invoice.model_validate(INVOICE)
    position = InvoicePosition.model_validate(POSITION)
    part = Part.model_validate({"id": "31", "name": "Maths", "price": None, "taxRate": "0.0", "status": "100"})

    assert (invoice.id, invoice.status, invoice.contact.id, invoice.small_settlement) == (501, 100, 21, True)
    assert invoice.sum_net == Decimal("45.00")
    assert invoice.tax_rule is not None and invoice.tax_rule.id == 11
    assert (position.quantity, position.price, position.position_number) == (Decimal("1.5"), Decimal(30), 0)
    assert Invoice.model_validate({**INVOICE, "smallSettlement": "0"}).small_settlement is False
    assert (part.price, part.tax_rate, part.status) == (None, Decimal(0), 100)


def test_a_reference_typed_as_a_list_of_one_is_accepted():
    field = ContactCustomField.model_validate({
        "id": "1",
        "contact": {"id": "21", "objectName": "Contact"},
        "contactCustomFieldSetting": [{"id": "13", "objectName": "ContactCustomFieldSetting"}],
        "value": "synthetic",
    })

    assert field.contact_custom_field_setting.id == 13


@pytest.mark.parametrize("bad", [{"status": "draft"}, {"contact": None}, {"id": "five"}])
def test_a_wrong_type_is_rejected(bad):
    with pytest.raises(ValueError):
        Invoice.model_validate({**INVOICE, **bad})


# --- reads ---


async def test_requests_carry_the_raw_token_and_neutral_headers(settings, fake_sevdesk: FakeSevdesk):
    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport):
        pass

    headers = fake_sevdesk.requests[0].headers
    assert headers["authorization"] == TOKEN
    assert (headers["accept"], headers["user-agent"]) == ("application/json", "skillforge")
    assert str(fake_sevdesk.requests[0].url) == "https://my.sevdesk.de/api/v1/SevClient"


async def test_an_unknown_id_reads_as_none(settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/Contact/22", objects([]))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        assert await client.get_contact(21) is None  # the fake answers 404
        assert await client.get_contact(22) is None


async def test_a_single_object_reads_as_a_list_of_one(settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/Invoice/501", objects(INVOICE))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        invoice = await client.get_invoice(501)

    assert invoice is not None and invoice.id == 501


async def test_find_invoices_pages_and_filters_by_the_note_itself(settings, fake_sevdesk: FakeSevdesk, monkeypatch):
    monkeypatch.setattr(sevdesk_client, "PAGE_LIMIT", 2)
    other = {**INVOICE, "id": "502", "customerInternalNote": "skillforge:run-0002"}
    pages = {"0": [INVOICE, other], "2": [{**INVOICE, "id": "503"}]}
    fake_sevdesk.on("GET", "/Invoice", lambda request: objects(pages[request.url.params["offset"]]))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        found = await client.find_invoices("skillforge:run-0001")

    assert [invoice.id for invoice in found] == [501, 503]
    params = [dict(request.url.params) for request in fake_sevdesk.requests[1:]]
    assert params == [
        {"customerInternalNote": "skillforge:run-0001", "limit": "2", "offset": "0"},
        {"customerInternalNote": "skillforge:run-0001", "limit": "2", "offset": "2"},
    ]


async def test_an_unexpected_read_names_fields_but_no_values(settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/Invoice/501", objects([{**INVOICE, "status": "Synthetic Person"}]))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        with pytest.raises(UnexpectedResponseError, match=r"\['status'\]") as raised:
            await client.get_invoice(501)

    assert "Synthetic Person" not in str(raised.value)


async def test_a_read_failing_with_500_is_unavailable_and_keeps_the_remote_message(settings, fake_sevdesk):
    fake_sevdesk.on("GET", "/Part", httpx.Response(500, json={"error": {"message": "Synthetic failure"}}))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        with pytest.raises(IntegrationUnavailableError) as raised:
            await client.parts()

    assert raised.value.remote_message == "Synthetic failure"


# --- the saveInvoice write ---


async def test_a_dry_run_save_returns_the_planned_request_and_sends_no_post(settings, fake_sevdesk: FakeSevdesk):
    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        planned = await client.save_invoice(DRAFT)

    assert isinstance(planned, PlannedRequest)
    assert (planned.method, planned.url) == ("POST", "https://my.sevdesk.de/api/v1/Invoice/Factory/saveInvoice")
    assert fake_sevdesk.sent("POST") == []
    assert fake_sevdesk.sent() == ["GET /SevClient"]


async def test_the_save_body_is_a_draft_under_section_19(settings, fake_sevdesk: FakeSevdesk):
    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        planned = await client.save_invoice(DRAFT)

    assert isinstance(planned, PlannedRequest) and isinstance(planned.body, dict)
    body = planned.body
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
        "contact": {"id": 21, "objectName": "Contact"},
        "contactPerson": {"id": 11, "objectName": "SevUser"},
        "status": 100,
        "invoiceType": "RE",
        "currency": "EUR",
        "taxRule": {"id": "11", "objectName": "TaxRule"},
        "taxText": "Synthetic tax text",
        "header": "Invoice 2026-10",
        "footText": "Synthetic foot text",
        "customerInternalNote": "skillforge:run-0001",
    }
    assert body["invoicePosSave"] == [
        {
            "objectName": "InvoicePos",
            "mapAll": True,
            "name": "Maths",
            "quantity": 1.5,
            "price": 30.0,
            "taxRate": 0,
            "unity": {"id": 12, "objectName": "Unity"},
            "part": {"id": 31, "objectName": "Part"},
        }
    ]
    assert (body["invoicePosDelete"], body["discountSave"], body["discountDelete"]) == (None, None, None)
    assert body["takeDefaultAddress"] is True


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


SAVE_FAILURES = {
    "a timeout": httpx.ReadTimeout("timed out"),
    "a dropped connection": httpx.RemoteProtocolError("closed"),
    "a refused connection": httpx.ConnectError("refused"),
    "a 500 validation error": httpx.Response(500, json={"error": {"message": "Synthetic validation error"}}),
    "an unreadable success": httpx.Response(201, json={"objects": {"invoice": {"id": "501"}}}),
}


@pytest.mark.parametrize("answer", SAVE_FAILURES.values(), ids=SAVE_FAILURES.keys())
async def test_a_failed_save_is_sent_once_and_its_outcome_unknown(answer, live_settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("POST", "/Invoice/Factory/saveInvoice", answer)

    async with SevdeskClient.connect(live_settings, transport=fake_sevdesk.transport) as client:
        with pytest.raises(OutcomeUnknownError):
            await client.save_invoice(DRAFT)

    assert fake_sevdesk.sent("POST") == ["POST /Invoice/Factory/saveInvoice"]


# --- the token stays inside ---


@pytest.fixture
def restore_logging():
    yield
    configure_logging(LoggingSettings())


async def test_the_token_appears_in_no_repr_log_or_error(
    live_settings, fake_sevdesk: FakeSevdesk, restore_logging, capsys, caplog
):
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()
    fake_sevdesk.on("POST", "/Invoice/Factory/saveInvoice", httpx.Response(500, json={"error": {"message": "x"}}))
    fake_sevdesk.on("GET", "/Part", httpx.ReadTimeout("timed out"))
    errors: list[Exception] = []

    async with SevdeskClient.connect(live_settings, transport=fake_sevdesk.transport) as client:
        for call in (client.save_invoice(DRAFT), client.parts()):
            try:
                await call
            except (OutcomeUnknownError, IntegrationUnavailableError) as exc:
                errors.append(exc)
        texts = [repr(client), repr(live_settings), str(live_settings)]

    output = capsys.readouterr().out
    assert len(errors) == 2 and output.count("integration_request") == 3
    for text in (*texts, output, caplog.text, *map(str, errors), *map(repr, errors)):
        assert TOKEN not in text
