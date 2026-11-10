"""The sevDesk client against a synthetic sevDesk: wire format, lenient parsing, complete listings, no leaks."""

import traceback
from decimal import Decimal

import httpx
import pytest
from skillcore.logging.redaction import REDACTED

from app.core.logging import LogFormat, LoggingSettings, LogLevel, configure_logging
from app.integrations.errors import (
    IntegrationError,
    IntegrationRejectedError,
    IntegrationUnavailableError,
    UnexpectedResponseError,
)
from app.integrations.sevdesk import client as sevdesk_client
from app.integrations.sevdesk.client import SevdeskClient
from app.integrations.sevdesk.models import Account, Contact, ContactCustomField, Part
from app.integrations.transport import Exchange, Reply
from tests.integrations.sevdesk.fake_sevdesk import ACCOUNT_ID, TOKEN, FakeSevdesk, objects, page

PART = {"id": "31", "objectName": "Part", "name": "Maths", "price": "30", "taxRate": "0.0", "status": "100"}


# --- lenient parsing, strict types ---


def test_string_numbers_parse_into_strict_types():
    part = Part.model_validate(PART)
    contact = Contact.model_validate({"id": "21", "category": {"id": "3", "objectName": "Category"}})

    assert (part.id, part.price_cents, part.tax_rate, part.status) == (31, 3000, Decimal(0), 100)
    assert type(part.price_cents) is int
    assert Part.model_validate({**PART, "price": None}).price_cents is None
    assert contact.category is not None and (contact.id, contact.category.id) == (21, 3)


@pytest.mark.parametrize(
    ("euros", "cents"),
    [
        ("30", 3000),
        ("30.00", 3000),
        ("29.99", 2999),
        ("0.25", 25),
        ("-1.50", -150),
        (30, 3000),
        (Decimal("30.10"), 3010),
        ("90071992547409.93", 9007199254740993),
        (Decimal("90071992547409.93"), 9007199254740993),
        ("9999999999999999999999999.99", 999999999999999999999999999),
    ],
)
def test_an_amount_parses_into_exact_int_cents(euros: object, cents: int):
    price = Part.model_validate({**PART, "price": euros}).price_cents

    assert price == cents and type(price) is int


def test_an_amount_read_from_a_response_body_stays_exact_above_2_to_the_53():
    body = Reply(200, b'{"id": 31, "name": "Maths", "price": 90071992547409.93}').json()

    assert Part.model_validate(body).price_cents == 9007199254740993 == 2**53 + 1


@pytest.mark.parametrize("amount", ["30.005", "0.001", Decimal("0.001"), "1e-3"])
def test_an_amount_below_a_cent_is_refused(amount: object):
    with pytest.raises(ValueError, match="below a cent"):
        Part.model_validate({**PART, "price": amount})


@pytest.mark.parametrize(
    ("amount", "message"),
    [
        (30.0, "must not pass through float"),
        (0.1 + 0.2, "must not pass through float"),
        (True, "decimal string or a number"),
        ([30], "decimal string or a number"),
        ("thirty", "exact decimal number"),
        ("sNaN", "exact decimal number"),
        ("1" * 40 + ".01", "exact decimal number"),
        ("NaN", "exact decimal number"),
        ("Infinity", "exact decimal number"),
    ],
)
def test_a_float_or_an_inexact_amount_is_refused(amount: object, message: str):
    with pytest.raises(ValueError, match=message):
        Part.model_validate({**PART, "price": amount})


def test_a_reference_typed_as_a_list_of_one_is_accepted():
    field = ContactCustomField.model_validate({
        "id": "1",
        "contact": {"id": "21", "objectName": "Contact"},
        "contactCustomFieldSetting": [{"id": "13", "objectName": "ContactCustomFieldSetting"}],
        "value": "synthetic",
    })

    assert field.contact_custom_field_setting.id == 13


@pytest.mark.parametrize("bad", [{"status": "draft"}, {"name": None}, {"id": "five"}])
def test_a_wrong_type_is_rejected(bad):
    with pytest.raises(ValueError):
        Part.model_validate({**PART, **bad})


# --- reads ---


async def test_requests_carry_the_raw_token_and_neutral_headers(settings, fake_sevdesk: FakeSevdesk):
    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport):
        pass

    headers = fake_sevdesk.requests[0].headers
    assert headers["authorization"] == TOKEN
    assert (headers["accept"], headers["user-agent"]) == ("application/json", "skillforge")
    assert str(fake_sevdesk.requests[0].url) == "https://my.sevdesk.de/api/v1/SevClient"


async def test_an_observer_sees_the_token_redacted(settings, fake_sevdesk: FakeSevdesk):
    exchanges: list[Exchange] = []

    async with SevdeskClient.connect(settings, observer=exchanges.append, transport=fake_sevdesk.transport):
        pass

    assert [exchange.request_headers["authorization"] for exchange in exchanges] == [REDACTED]
    assert exchanges[0].request_headers["accept"] == "application/json"


async def test_an_unknown_id_reads_as_none(settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/Contact/22", objects([]))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        assert await client.get_contact(21) is None  # the fake answers 404
        assert await client.get_contact(22) is None


WRONG_OBJECTS = {
    "another object": objects([{"id": "22", "objectName": "Contact"}]),
    "two objects": objects([{"id": "21", "objectName": "Contact"}, {"id": "22", "objectName": "Contact"}]),
    "the object twice": objects([{"id": "21", "objectName": "Contact"}, {"id": "21", "objectName": "Contact"}]),
}


@pytest.mark.parametrize("answer", WRONG_OBJECTS.values(), ids=WRONG_OBJECTS.keys())
async def test_a_read_by_id_that_answers_another_or_several_objects_is_refused(answer, settings, fake_sevdesk):
    fake_sevdesk.on("GET", "/Contact/21", answer)

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        with pytest.raises(UnexpectedResponseError, match=r"GET /Contact/\{id\}: the answer is not the object"):
            await client.get_contact(21)


async def test_a_single_object_reads_as_a_list_of_one(settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/Contact/21", objects({"id": "21", "objectName": "Contact"}))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        contact = await client.get_contact(21)

    assert contact is not None and contact.id == 21


async def test_an_unexpected_read_names_fields_but_no_values(settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/Part", page([{**PART, "status": "Synthetic Person"}]))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        with pytest.raises(UnexpectedResponseError, match=r"\['status'\]") as raised:
            await client.parts()

    assert "Synthetic Person" not in str(raised.value)


async def test_a_read_failing_with_500_is_unavailable_and_keeps_the_remote_message(settings, fake_sevdesk):
    fake_sevdesk.on("GET", "/Part", httpx.Response(500, json={"error": {"message": "Synthetic failure"}}))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        with pytest.raises(IntegrationUnavailableError) as raised:
            await client.parts()

    assert raised.value.remote_message == "Synthetic failure"


async def test_a_rejection_other_than_404_is_no_unknown_id(settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/Contact/21", httpx.Response(401, json={"error": {"message": "Authentication required"}}))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        with pytest.raises(IntegrationRejectedError) as raised:
            await client.get_contact(21)

    assert raised.value.status == 401


async def test_the_configuration_reads(settings, fake_sevdesk: FakeSevdesk):
    user = {"id": "11", "objectName": "SevUser", "sevClient": {"id": str(ACCOUNT_ID), "objectName": "SevClient"}}
    setting = {"id": "13", "objectName": "ContactCustomFieldSetting", "name": "Payment method"}
    fake_sevdesk.on("GET", "/SevUser", objects([user]))
    fake_sevdesk.on("GET", "/SevUser/11", objects([user]))
    fake_sevdesk.on("GET", "/Unity/12", objects([{"id": "12", "objectName": "Unity", "name": "Std."}]))
    fake_sevdesk.on("GET", "/ContactCustomFieldSetting", objects([setting]))
    fake_sevdesk.on("GET", "/ContactCustomFieldSetting/13", objects([setting]))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        users, user_11 = await client.users(), await client.get_user(11)
        unity, settings_read = await client.get_unity(12), await client.custom_field_settings()
        setting_13 = await client.get_custom_field_setting(13)

    assert [found.id for found in users] == [11] and users[0].sev_client is not None
    assert user_11 is not None and user_11.sev_client is not None and user_11.sev_client.id == ACCOUNT_ID
    assert unity is not None and (unity.id, unity.name) == (12, "Std.")
    assert [found.name for found in settings_read] == ["Payment method"]
    assert setting_13 is not None and setting_13.id == 13


async def test_contacts_by_custom_field_send_the_field_and_value(settings, fake_sevdesk: FakeSevdesk):
    contact = {"id": "21", "objectName": "Contact", "customerNumber": "1001"}
    fake_sevdesk.on("GET", "/Contact/Factory/findContactsByCustomFieldValue", objects([contact]))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        found = await client.find_contacts_by_custom_field("skillforge_party", "party-0001")

    assert [(item.id, item.customer_number) for item in found] == [(21, "1001")]
    params = dict(fake_sevdesk.requests[-1].url.params)
    assert params == {"customFieldName": "skillforge_party", "value": "party-0001"}


async def test_custom_fields_of_another_contact_are_dropped(settings, fake_sevdesk: FakeSevdesk):
    def field(field_id: str, contact_id: str) -> dict[str, object]:
        return {
            "id": field_id,
            "contact": {"id": contact_id, "objectName": "Contact"},
            "contactCustomFieldSetting": {"id": "13", "objectName": "ContactCustomFieldSetting"},
            "value": "synthetic",
        }

    fake_sevdesk.on("GET", "/ContactCustomField", page([field("1", "21"), field("2", "22")]))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        fields = await client.contact_custom_fields(21)

    assert [item.id for item in fields] == [1]
    params = dict(fake_sevdesk.requests[-1].url.params)
    assert (params["contact[id]"], params["contact[objectName]"]) == ("21", "Contact")


def _part(part_id: int) -> dict[str, object]:
    return {"id": str(part_id), "objectName": "Part", "name": f"Part {part_id}"}


BROKEN_LISTINGS = {
    "a page longer than the limit": lambda request: page([_part(1), _part(2), _part(3)], total=3),
    "an id twice in a page": lambda request: page([_part(1), _part(1)], total=4),
    "an id again on the next page": lambda request: page([_part(1), _part(2)], total=4),
    "fewer objects than the total": lambda request: page([_part(1)], total=2),
    "more objects than the total": lambda request: page([_part(1), _part(2)], total=1),
    "no total": lambda request: objects([_part(1)]),
    "a negative total": lambda request: page([], total=-1),
    "pages without end": lambda request: page(
        [_part(int(request.url.params["offset"]) + 1), _part(int(request.url.params["offset"]) + 2)], total=99
    ),
}


DEEP = b"[" * 100_000 + b"]" * 100_000


@pytest.mark.parametrize("path", ["/Part", "/Contact/21"])
async def test_a_body_nested_too_deep_to_parse_is_unexpected(path: str, settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", path, httpx.Response(200, content=DEEP, headers={"content-type": "application/json"}))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        with pytest.raises(UnexpectedResponseError, match="the response is not JSON"):
            await (client.parts() if path == "/Part" else client.get_contact(21))


def test_an_account_hides_its_company_name_in_its_repr():
    account = Account.model_validate({"id": "4711", "objectName": "SevClient", "name": "Example Company TRIAL"})

    assert "Example" not in repr(account) and "Example" not in str(account)


async def test_a_listing_without_a_total_says_so(settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/Part", objects([_part(1)]))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        with pytest.raises(UnexpectedResponseError, match="GET /Part: listing without countAll total"):
            await client.parts()


@pytest.mark.parametrize("answer", BROKEN_LISTINGS.values(), ids=BROKEN_LISTINGS.keys())
async def test_an_incomplete_or_inconsistent_listing_is_refused(answer, settings, fake_sevdesk, monkeypatch):
    monkeypatch.setattr(sevdesk_client, "PAGE_LIMIT", 2)
    monkeypatch.setattr(sevdesk_client, "MAX_PAGES", 3)
    fake_sevdesk.on("GET", "/Part", answer)

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        with pytest.raises(UnexpectedResponseError):
            await client.parts()

    assert len(fake_sevdesk.sent()) <= 1 + 3


async def test_a_listing_stops_once_its_total_is_read(settings, fake_sevdesk: FakeSevdesk, monkeypatch):
    monkeypatch.setattr(sevdesk_client, "PAGE_LIMIT", 2)
    pages = {"0": [_part(1), _part(2)], "2": [_part(3), _part(4)]}
    fake_sevdesk.on("GET", "/Part", lambda request: page(pages[request.url.params["offset"]], total=4))

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        parts = await client.parts()

    assert [part.id for part in parts] == [1, 2, 3, 4]
    assert fake_sevdesk.sent() == ["GET /SevClient", "GET /Part", "GET /Part"]
    params = [dict(request.url.params) for request in fake_sevdesk.requests[1:]]
    assert params == [
        {"limit": "2", "offset": "0", "countAll": "true"},
        {"limit": "2", "offset": "2", "countAll": "true"},
    ]


# --- the token stays inside ---


@pytest.fixture
def restore_logging():
    yield
    configure_logging(LoggingSettings())


async def test_the_token_appears_in_no_repr_log_or_error(
    settings, fake_sevdesk: FakeSevdesk, restore_logging, capsys, caplog
):
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()
    fake_sevdesk.on("GET", "/Contact/21", httpx.Response(500, json={"error": {"message": "x"}}))
    fake_sevdesk.on("GET", "/Part", httpx.LocalProtocolError(f"Illegal header value b'{TOKEN} '"))
    errors: list[Exception] = []

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        for call in (client.get_contact(21), client.parts()):
            try:
                await call
            except IntegrationUnavailableError as exc:
                errors.append(exc)
        texts = [repr(client), repr(settings), str(settings)]

    output = capsys.readouterr().out
    assert len(errors) == 2 and output.count("integration_request") == 3
    tracebacks = ["".join(traceback.format_exception(error)) for error in errors]
    for text in (*texts, output, caplog.text, *tracebacks, *map(repr, errors)):
        assert TOKEN not in text


async def test_no_id_appears_in_a_log_line_or_an_error_message(
    settings, fake_sevdesk: FakeSevdesk, restore_logging, capsys, caplog
):
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()
    contact = 98765
    fake_sevdesk.on("GET", f"/Contact/{contact}", httpx.Response(500, json={"error": {"message": "x"}}))
    fake_sevdesk.on("GET", f"/SevUser/{contact}", objects([{"id": "1", "objectName": "SevUser"}]))
    fake_sevdesk.on("GET", f"/Unity/{contact}", objects([{"id": str(contact), "objectName": "Unity", "name": None}]))
    fake_sevdesk.on("GET", f"/ContactCustomFieldSetting/{contact}", httpx.Response(401, json={}))
    fake_sevdesk.on("GET", "/ContactCustomField", page([]))
    errors: list[Exception] = []

    async with SevdeskClient.connect(settings, transport=fake_sevdesk.transport) as client:
        for call in (
            client.get_contact(contact),
            client.get_user(contact),
            client.get_unity(contact),
            client.get_custom_field_setting(contact),
            client.contact_custom_fields(contact),
        ):
            try:
                await call
            except IntegrationError as exc:
                errors.append(exc)

    output = capsys.readouterr().out
    assert [type(error) for error in errors] == [
        IntegrationUnavailableError,
        UnexpectedResponseError,
        UnexpectedResponseError,
        IntegrationRejectedError,
    ]
    assert output.count("integration_request") == 6 and "/api/v1/Contact/{id}" in output
    tracebacks = ["".join(traceback.format_exception(error)) for error in errors]
    for text in (output, caplog.text, *tracebacks, *map(repr, errors)):
        assert str(contact) not in text
