"""The recorder scrubs every sensitive class out of a fixture and keeps the shape the tests need."""

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.recording import RUN_ID as RUN_ID_FORMAT
from app.integrations.recording import SCRUBBED, Recorder, Scrubber, new_run_id
from app.integrations.transport import Exchange, IntegrationHttp
from tests.test_fixture_scan import leaks

TOKEN = "0123456789abcdef0123456789abcdef"
COMPANY = "Example Tutoring Company TRIAL"
RUN_ID = "run202611101200000042"
SENSITIVE = {
    "the token": TOKEN,
    "a session cookie": "session=synthetic-cookie",
    "the company": "Example Tutoring Company",
    "the trial marker": "TRIAL",
    "a first name": "Synthia",
    "a family name": "Examplova",
    "a street": "Example Street 1",
    "a postal code": "12345",
    "an IBAN": "DE89 3704 0044 0532 0130 00",
    "an IBAN without spaces": "DE89370400440532013000",
    "an e-mail address": "synthia@mail.example.com",
    "a phone number": "+49 30 1234567",
    "a national phone number": "030/1234567",
    "a VAT number": "DE123456789",
}
PDF = base64.b64encode(b"%PDF-1.7 synthetic " * 400).decode()


def _exchange() -> Exchange:
    response = {
        "objects": [
            {"id": "4711", "objectName": "SevClient", "name": COMPANY},
            {
                "id": "21",
                "objectName": "Contact",
                "surename": "Synthia",
                "familyname": "Examplova",
                "name": None,
                "vatNumber": "DE123456789",
                "bankAccount": "DE89 3704 0044 0532 0130 00",
                "sevClient": {"id": "4711", "objectName": "SevClient"},
            },
            {"id": "31", "objectName": "ContactAddress", "street": "Example Street 1", "zip": "12345"},
            {"id": "41", "objectName": "CommunicationWay", "type": "EMAIL", "value": "synthia@mail.example.com"},
            {
                "id": "501",
                "objectName": "Invoice",
                "invoiceDate": "2026-11-02T00:00:00+01:00",
                "sumGross": "45.00",
                "address": "Synthia Examplova\nExample Street 1\n12345 Example City",
                "footText": f"{COMPANY}, IBAN DE89370400440532013000, call 030/1234567 or +49 30 1234567",
                "customerInternalNote": f"run {TOKEN}",
            },
            {"id": "61", "objectName": "Part", "name": "Maths", "unity": {"id": "12", "objectName": "Unity"}},
            {"filename": "RE-1001.pdf", "mimeType": "application/pdf", "content": PDF},
        ]
    }
    return Exchange(
        method="POST",
        url="https://my.sevdesk.de/api/v1/Contact?value=synthia%40mail.example.com&limit=100",
        request_headers={"authorization": TOKEN, "cookie": "session=synthetic-cookie", "accept": "application/json"},
        request_body={"familyname": RUN_ID, "surename": "Spike", "category": {"id": 3}},
        status=200,
        response_headers={"content-type": "application/json", "set-cookie": "session=synthetic-cookie"},
        response_content=json.dumps(response).encode(),
    )


def _recorder(directory: Path = Path("unused")) -> Recorder:
    return Recorder(directory, run_id=RUN_ID)


def _recorded(exchange: Exchange, recorder: Recorder | None = None):
    """The record as a fixture reader sees it: plain JSON."""
    return json.loads(json.dumps((recorder or _recorder()).record(exchange)))


def test_the_scrub_removes_every_sensitive_class():
    recorded = json.dumps(_recorder().record(_exchange()), ensure_ascii=False)

    assert {name for name, value in SENSITIVE.items() if value in recorded} == set()
    assert leaks(recorded) == []


def test_the_scrub_keeps_what_the_tests_need():
    record = _recorded(_exchange())
    objects = record["response"]["body"]["objects"]

    assert record["request"]["params"] == {"value": "person@example.org", "limit": "100"}
    assert record["request"]["body"] == {"familyname": RUN_ID, "surename": "Spike", "category": {"id": 3}}
    assert record["request"]["headers"] == {"accept": "application/json"}
    assert objects[0] == {"id": "0", "objectName": "SevClient", "name": SCRUBBED}
    assert objects[1]["sevClient"] == {"id": "0", "objectName": "SevClient"}
    assert objects[1]["name"] is None
    assert objects[3] == {"id": "41", "objectName": "CommunicationWay", "type": "EMAIL", "value": SCRUBBED}
    assert (objects[4]["id"], objects[4]["invoiceDate"], objects[4]["sumGross"]) == (
        "501",
        "2026-11-02T00:00:00+01:00",
        "45.00",
    )
    assert (objects[4]["footText"], objects[4]["customerInternalNote"]) == (SCRUBBED, SCRUBBED)
    assert objects[5]["name"] == "Maths"
    assert objects[6]["content"] == f"[{len(PDF)} characters]"


def test_a_binary_response_is_recorded_by_size():
    pdf = replace(_exchange(), response_content=b"%PDF-1.7", response_headers={})

    assert _recorded(pdf)["response"]["body"] == "[8 bytes ]"


def test_a_value_a_response_revealed_is_scrubbed_wherever_it_shows_up_later():
    scrub = Scrubber()
    scrub.learn({"objectName": "SevClient", "name": COMPANY})

    assert scrub({"objectName": "Part", "name": f"Course by {COMPANY}"}) == {
        "objectName": "Part",
        "name": f"Course by {SCRUBBED}",
    }


# --- people: an allowlist per type, at every depth ---

PEOPLE_TYPES = ["SevClient", "SevUser", "Contact", "ContactAddress", "CommunicationWay"]


@pytest.mark.parametrize("object_name", PEOPLE_TYPES)
def test_a_person_keeps_only_structure_references_and_timestamps(object_name: str):
    scrub = Scrubber()
    scrub.sent({"familyname": RUN_ID, "header": f"Spike {RUN_ID}"})
    person = {
        "id": "21",
        "objectName": object_name,
        "create": "2026-11-02T10:00:00+01:00",
        "update": "Synthia Examplova",
        "district": "Example District",
        "favouriteColour": "green",
        "familyname": RUN_ID,
        "remark": f"Spike {RUN_ID}",
        "header": f"Spike {RUN_ID}",
        "age": 41,
        "active": True,
        "parent": {"id": "20", "objectName": "Category"},
        "settings": {"signatory": "Synthia Examplova", "deep": {"owner": "Examplova Family", "id": "9"}},
        "aliases": ["Synthia", {"nick": "Syn"}],
    }

    scrubbed = scrub(person)

    assert scrubbed == {
        "id": "0" if object_name == "SevClient" else "21",
        "objectName": object_name,
        "create": "2026-11-02T10:00:00+01:00",
        "update": SCRUBBED,
        "district": SCRUBBED,
        "favouriteColour": SCRUBBED,
        "familyname": SCRUBBED,
        "remark": SCRUBBED,
        "header": SCRUBBED,
        "age": SCRUBBED,
        "active": SCRUBBED,
        "parent": {"id": "20", "objectName": "Category"},
        "settings": {"signatory": SCRUBBED, "deep": {"owner": SCRUBBED, "id": "9"}},
        "aliases": [SCRUBBED, {"nick": SCRUBBED}],
    }


def test_the_few_non_personal_fields_of_a_contact_and_a_communication_way_stay():
    scrubbed = Scrubber()([
        {"id": "21", "objectName": "Contact", "customerNumber": "1001", "status": "100", "surename": "Synthia"},
        {"id": "41", "objectName": "CommunicationWay", "type": "PHONE", "main": "1", "value": "+49 30 1234567"},
        {"id": "31", "objectName": "ContactAddress", "customerNumber": "1001", "status": "100", "type": "x"},
    ])

    assert scrubbed == [
        {"id": "21", "objectName": "Contact", "customerNumber": "1001", "status": "100", "surename": SCRUBBED},
        {"id": "41", "objectName": "CommunicationWay", "type": "PHONE", "main": "1", "value": SCRUBBED},
        {"id": "31", "objectName": "ContactAddress", "customerNumber": SCRUBBED, "status": SCRUBBED, "type": SCRUBBED},
    ]


def test_what_a_person_holds_is_scrubbed_wherever_it_shows_up_later():
    scrub = Scrubber()
    scrub.learn({"objects": [{"objectName": "Contact", "id": "21", "settings": {"signatory": "Synthia Examplova"}}]})

    assert scrub.text("Regards, Synthia Examplova") == f"Regards, {SCRUBBED}"


# --- other objects: free text goes unless the run sent exactly that ---


def test_a_text_the_run_sent_stays_and_any_other_goes():
    scrub = Scrubber()
    sent = {
        "header": f"Spike {RUN_ID}",
        "customerInternalNote": f"skillforge-spike:{RUN_ID}",
        "footText": f"Spike {RUN_ID}: <strong>Zahlungsverfahren:</strong> PayPal<br>[%KONTAKTPERSON%]",
        "taxText": f"Spike {RUN_ID} tax text",
    }
    scrub.sent({"invoice": sent, "invoicePosSave": [{"name": f"Spike {RUN_ID} position"}]})
    scrub.sent({"surename": "Spike", "familyname": RUN_ID})
    scrub.learn({"objectName": "Contact", "surename": "Spike", "familyname": RUN_ID, "nick": "Synthia"})

    stored = scrub({
        "objectName": "Invoice",
        **sent,
        "headText": f"Spike {RUN_ID}: Synthia Examplova, thank you",
        "address": f"Spike {RUN_ID}\nExample Street 1",
        "positions": [{"objectName": "InvoicePos", "name": f"Spike {RUN_ID} position", "text": "Synthia"}],
    })

    assert stored == {
        "objectName": "Invoice",
        **sent,
        "headText": SCRUBBED,
        "address": SCRUBBED,
        "positions": [{"objectName": "InvoicePos", "name": f"Spike {RUN_ID} position", "text": SCRUBBED}],
    }


def test_nothing_is_learned_from_what_the_run_sent():
    recorder = _recorder()
    exchange = replace(
        _exchange(),
        url="https://my.sevdesk.de/api/v1/Contact/Factory/findContactsByCustomFieldValue?customFieldName=Zahlungsverfahren&value=PayPal",
        request_body=None,
        response_content=json.dumps({"objects": [{"id": "21", "objectName": "Contact"}]}).encode(),
    )
    recorder.record(exchange)

    setting = {"objectName": "ContactCustomFieldSetting", "name": "Zahlungsverfahren", "description": "PayPal"}
    assert _recorded(replace(exchange, response_content=json.dumps(setting).encode()), recorder)["response"][
        "body"
    ] == (setting)


def test_a_placeholder_stays_and_is_never_learned():
    scrub = Scrubber()
    foot_text = f"Spike {RUN_ID}: <strong>bis zum [%ZAHLUNGSZIEL%]</strong><br>[%KONTAKTPERSON%]"
    scrub.sent({"footText": foot_text})
    dictionary = [
        {"key": "Invoice", "value": [{"key": "INVOICE_NUMBER", "value": "[%RECHNUNGSNUMMER%]"}]},
        {"key": "Contact", "value": [{"key": "CONTACT_PERSON", "value": "[%KONTAKTPERSON%]"}]},
        {"key": "Contact", "value": [{"key": "PAYMENT", "value": "[%KONTAKT.ZAHLUNGSVERFAHREN%]"}]},
    ]
    person = {"objectName": "SevUser", "id": "11", "salutation": "[%ANREDE%]", "firstName": "Synthia"}

    scrub.learn([dictionary, person])

    assert scrub(dictionary) == dictionary
    assert scrub(person) == {"objectName": "SevUser", "id": "11", "salutation": "[%ANREDE%]", "firstName": SCRUBBED}
    assert scrub({"objectName": "Invoice", "footText": foot_text}) == {"objectName": "Invoice", "footText": foot_text}
    other = {"objectName": "Invoice", "footText": "[%RECHNUNGSNUMMER%] Synthia", "headText": "[%synthia%]"}
    assert scrub(other) == {"objectName": "Invoice", "footText": SCRUBBED, "headText": SCRUBBED}, (
        "a value that holds more than a placeholder, or a lower-case one, is free text"
    )


def test_payment_methods_and_configuration_names_stay():
    scrubbed = Scrubber()([
        {"objectName": "ContactCustomField", "value": "PayPal", "contact": {"id": "21", "objectName": "Contact"}},
        {"objectName": "ContactCustomField", "value": "Überweisung"},
        {"objectName": "ContactCustomField", "value": "Synthia's notes"},
        {"objectName": "Unity", "name": "Std."},
        {"objectName": "Part", "name": "Maths"},
        {"objectName": "ContactCustomFieldSetting", "name": "Zahlungsverfahren"},
        {"objectName": "InvoicePos", "name": "Maths for Synthia Examplova", "quantity": "1.5"},
        {"objectName": "Invoice", "addressParentName": "Synthia Examplova", "sumTax": "0.00"},
    ])

    assert scrubbed == [
        {"objectName": "ContactCustomField", "value": "PayPal", "contact": {"id": "21", "objectName": "Contact"}},
        {"objectName": "ContactCustomField", "value": "Überweisung"},
        {"objectName": "ContactCustomField", "value": SCRUBBED},
        {"objectName": "Unity", "name": "Std."},
        {"objectName": "Part", "name": "Maths"},
        {"objectName": "ContactCustomFieldSetting", "name": "Zahlungsverfahren"},
        {"objectName": "InvoicePos", "name": SCRUBBED, "quantity": "1.5"},
        {"objectName": "Invoice", "addressParentName": SCRUBBED, "sumTax": "0.00"},
    ]


# --- patterns in every string ---

PATTERNS = {
    "a phone in brackets": "(0170) 1234567",
    "an international phone": "+49 (0) 170 1234567",
    "a phone after a label": "Tel.:+491701234567",
    "a phone with dots": "0170.1234567",
    "a phone with a slash": "030/1234567",
    "a phone after a date": "01.10.2026 0170 1234567",
    "a url-encoded e-mail": "pay at https://pay.test/?m=synthia%40mail.example.com",
    "an e-mail in capitals": "SYNTHIA@MAIL.EXAMPLE.COM",
    "an IBAN with no-break spaces": "DE89 3704 0044 0532 0130 00",
    "a glued token": f"tok{TOKEN}",
    "a short base64 blob": base64.b64encode(b"Synthia Examplova, Example Street 1 " * 8).decode(),
}


def test_every_pattern_is_scrubbed_in_any_string():
    scrub = Scrubber()

    for name, value in PATTERNS.items():
        scrubbed = scrub.text(f"see {value} here")
        assert value not in scrubbed and leaks(scrubbed) == [], name
        assert not any(part in scrubbed for part in ("1234567", "Synthia", "0532", "MAIL.EXAMPLE", TOKEN)), name


@pytest.mark.parametrize(
    "iban", ["gb82 west 1234 5698 7654 32", "GB82 WEST 1234 5698 7654 32", "gb82west12345698765432"]
)
def test_an_iban_in_any_case_is_scrubbed_by_the_iban_rule(iban: str):
    """An IBAN whose digits the phone rule cannot catch: only the IBAN rule scrubs it."""
    assert Scrubber().text(f"pay to {iban}.") == "pay to [IBAN]."


def test_a_long_text_is_recorded_by_size_only():
    text = "Synthia Examplova wrote a long letter. " * 60

    assert Scrubber().text(text) == f"[{len(text)} characters]"


@pytest.mark.parametrize(
    "text",
    [
        f"period 01.10.2026 to 31.10.2026, sum 1234567.89, run {RUN_ID}, at 2026-11-02T00:00:00+01:00",
        "Leistungszeitraum 01.10.2026 - 31.10.2026",
        "Leistungszeitraum 01.10.2026-31.10.2026",
        "from 2026-10-01 - 2026-10-31",
        f"Spike {RUN_ID}: 0042 lessons",
    ],
)
def test_dates_date_ranges_amounts_and_the_run_id_are_no_phone_number(text: str):
    assert Scrubber().text(text) == text
    assert leaks(text) == []


def test_keys_and_the_request_path_are_scrubbed_too():
    exchange = replace(
        _exchange(),
        url="https://my.sevdesk.de/api/v1/Contact/Factory/byEmail/synthia@mail.example.com",
        request_body={"synthia@mail.example.com": 1},
    )

    record = _recorded(exchange)

    assert record["request"]["path"] == "/api/v1/Contact/Factory/byEmail/person@example.org"
    assert record["request"]["body"] == {"person@example.org": 1}


def test_what_a_response_reveals_is_scrubbed_from_its_own_request_path():
    exchange = replace(_exchange(), url="https://my.sevdesk.de/api/v1/Contact/Factory/byName/Examplova")

    assert _recorded(exchange)["request"]["path"] == f"/api/v1/Contact/Factory/byName/{SCRUBBED}"


# --- the recorder: armed after the identity check, one directory per run, never overwriting ---


def test_a_new_run_id_has_the_run_id_format():
    run_id = new_run_id(datetime(2026, 11, 10, 12, 0, tzinfo=UTC))

    assert RUN_ID_FORMAT.fullmatch(run_id) and run_id.startswith("run20261110120000")


@pytest.mark.parametrize("run_id", ["a", "20261110T120000-ab12", "run123", f"{RUN_ID}x", f"x{RUN_ID}", "run-../x"])
def test_a_run_id_outside_the_format_is_refused(run_id: str, tmp_path: Path):
    with pytest.raises(ValueError, match="run id"):
        Recorder(tmp_path, run_id=run_id)


async def test_the_recorder_writes_one_fixture_per_exchange_into_the_run_directory(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SKILLFORGE_CONTRACT_RUN", "1")
    recorder = _recorder(tmp_path / "sevdesk")
    recorder.arm()
    recorder.step = "version"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"objects": {"version": "2.0", "objectName": "Contact", "name": "Examplova"}})

    async with IntegrationHttp(
        name="sevdesk",
        base_url="https://my.sevdesk.de/api/v1",
        allowed_hosts=frozenset({"my.sevdesk.de"}),
        credentials={"Authorization": TOKEN},
        mode=IntegrationMode.LIVE,
        account=IntegrationAccount.TRIAL,
        observer=recorder,
        transport=httpx.MockTransport(handler),
    ) as http:
        await http.read("/Tools/bookkeepingSystemVersion")
        await http.write("POST", "/Contact", json={"familyname": RUN_ID})

    directory = tmp_path / "sevdesk" / RUN_ID
    assert sorted(path.name for path in directory.iterdir()) == ["version-01.json", "version-02.json"]
    for path in directory.iterdir():
        text = path.read_text()
        assert TOKEN not in text and "Examplova" not in text and leaks(text) == []


def test_a_run_continued_with_its_id_numbers_on_and_never_overwrites(tmp_path: Path):
    first = _recorder(tmp_path)
    first.arm()
    first(_exchange())
    again = _recorder(tmp_path)
    again.arm()
    again.step = "read-back"
    again(_exchange())

    assert sorted(path.name for path in (tmp_path / RUN_ID).iterdir()) == ["exchange-01.json", "read-back-02.json"]


def test_an_existing_fixture_is_never_overwritten_and_the_failure_is_kept(tmp_path: Path):
    recorder = _recorder(tmp_path)
    recorder.arm()
    (tmp_path / RUN_ID).mkdir()
    recorder._count = 0
    (tmp_path / RUN_ID / "exchange-01.json").write_text("recorded before\n")

    with pytest.raises(FileExistsError):
        recorder(_exchange())

    assert recorder.failure == "FileExistsError"
    assert (tmp_path / RUN_ID / "exchange-01.json").read_text() == "recorded before\n"


async def test_an_unarmed_recorder_writes_nothing(tmp_path: Path):
    recorder = _recorder(tmp_path / "sevdesk")

    recorder(_exchange())

    assert not (tmp_path / "sevdesk").exists()
    recorder.arm()
    recorder(_exchange())
    assert [path.name for path in (tmp_path / "sevdesk" / RUN_ID).iterdir()] == ["exchange-01.json"]
