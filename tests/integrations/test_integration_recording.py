"""The recorder scrubs every sensitive class out of a fixture and keeps the shape the tests need."""

import base64
import json
from dataclasses import replace
from pathlib import Path

import httpx

from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.recording import Recorder, Scrubber
from app.integrations.transport import Exchange, IntegrationHttp
from tests.test_fixture_scan import leaks

TOKEN = "0123456789abcdef0123456789abcdef"
COMPANY = "Example Tutoring Company TRIAL"
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
        request_body={"familyname": "Examplova", "surename": "Synthia", "category": {"id": 3}},
        status=200,
        response_headers={"content-type": "application/json", "set-cookie": "session=synthetic-cookie"},
        response_content=json.dumps(response).encode(),
    )


def test_the_scrub_removes_every_sensitive_class():
    recorded = json.dumps(Recorder(Path("unused")).record(_exchange()), ensure_ascii=False)

    assert {name for name, value in SENSITIVE.items() if value in recorded} == set()
    assert leaks(recorded) == []


def _recorded(exchange: Exchange):
    """The record as a fixture reader sees it: plain JSON."""
    return json.loads(json.dumps(Recorder(Path("unused")).record(exchange)))


def test_the_scrub_keeps_what_the_tests_need():
    record = _recorded(_exchange())
    objects = record["response"]["body"]["objects"]

    assert record["request"]["params"] == {"value": "[SCRUBBED]", "limit": "100"}
    assert record["request"]["headers"] == {"accept": "application/json"}
    assert objects[0] == {"id": "0", "objectName": "SevClient", "name": "[SCRUBBED]"}
    assert objects[1]["sevClient"] == {"id": "0", "objectName": "SevClient"}
    assert (objects[4]["id"], objects[4]["invoiceDate"], objects[4]["sumGross"]) == (
        "501",
        "2026-11-02T00:00:00+01:00",
        "45.00",
    )
    assert (objects[4]["footText"], objects[4]["customerInternalNote"]) == ("[SCRUBBED]", "[SCRUBBED]")
    assert objects[5]["name"] == "Maths"
    assert objects[6]["content"] == f"[{len(PDF)} characters]"


def test_a_binary_response_is_recorded_by_size():
    pdf = replace(_exchange(), response_content=b"%PDF-1.7", response_headers={})

    assert _recorded(pdf)["response"]["body"] == "[8 bytes ]"


def test_a_value_scrubbed_once_is_scrubbed_in_every_later_exchange():
    scrub = Scrubber()
    scrub({"objectName": "SevClient", "name": COMPANY})

    assert scrub({"objectName": "Part", "name": f"Course by {COMPANY}"}) == {
        "objectName": "Part",
        "name": "Course by [SCRUBBED]",
    }


async def test_the_recorder_writes_one_fixture_per_exchange(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SKILLFORGE_CONTRACT_RUN", "1")
    recorder = Recorder(tmp_path / "sevdesk")
    recorder.arm()
    recorder.step = "version"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"objects": {"version": "2.0"}})

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
        await http.write("POST", "/Contact", json={"familyname": "Examplova"})

    files = sorted(path.name for path in (tmp_path / "sevdesk").iterdir())
    assert files == ["version-01.json", "version-02.json"]
    for name in files:
        text = (tmp_path / "sevdesk" / name).read_text()
        assert TOKEN not in text and "Examplova" not in text and leaks(text) == []


RUN_ID = "20261110T120000-ab12"


def test_a_free_text_with_the_run_id_is_kept_and_its_patterns_scrubbed():
    scrub = Scrubber(keep=RUN_ID)
    scrub({"objectName": "SevClient", "id": "4711", "name": COMPANY})
    invoice = {
        "objectName": "Invoice",
        "header": f"Spike {RUN_ID}",
        "customerInternalNote": f"skillforge-spike:{RUN_ID}",
        "footText": f"Spike {RUN_ID}: {COMPANY}, IBAN DE89370400440532013000, call 030/1234567, token {TOKEN}",
        "headText": "Synthia Examplova, thank you",
    }

    assert scrub(invoice) == {
        "objectName": "Invoice",
        "header": f"Spike {RUN_ID}",
        "customerInternalNote": f"skillforge-spike:{RUN_ID}",
        "footText": f"Spike {RUN_ID}: [SCRUBBED], IBAN [IBAN], call [PHONE], token [TOKEN]",
        "headText": "[SCRUBBED]",
    }


def test_people_keep_only_ids_references_and_timestamps():
    people = [
        {
            "id": "31",
            "objectName": "ContactAddress",
            "create": "2026-11-02T10:00:00+01:00",
            "name": "Synthia Examplova",
            "name2": "Examplova Family",
            "name3": "c/o Synthia Examplova",
            "name4": "Synthia",
            "contact": {"id": "21", "objectName": "Contact"},
        },
        {"id": "21", "objectName": "Contact", "description": "Mother: Synthia Examplova", "customerNumber": "1001"},
        {"id": "4711", "objectName": "SevClient", "ceoName": "Synthia Examplova", "bankIban": "DE89370400440532013000"},
        {"id": "11", "objectName": "SevUser", "emailSignature": "Regards, Ms Examplova", "birthday": "1990-01-01"},
        {"id": "41", "objectName": "CommunicationWay", "key": {"id": "2", "objectName": "CommunicationWayKey"}},
    ]

    scrubbed = Scrubber()(people)

    assert scrubbed == [
        {
            "id": "31",
            "objectName": "ContactAddress",
            "create": "2026-11-02T10:00:00+01:00",
            "name": "[SCRUBBED]",
            "name2": "[SCRUBBED]",
            "name3": "[SCRUBBED]",
            "name4": "[SCRUBBED]",
            "contact": {"id": "21", "objectName": "Contact"},
        },
        {"id": "21", "objectName": "Contact", "description": "[SCRUBBED]", "customerNumber": "[SCRUBBED]"},
        {"id": "0", "objectName": "SevClient", "ceoName": "[SCRUBBED]", "bankIban": "[SCRUBBED]"},
        {"id": "11", "objectName": "SevUser", "emailSignature": "[SCRUBBED]", "birthday": "[SCRUBBED]"},
        {"id": "41", "objectName": "CommunicationWay", "key": {"id": "2", "objectName": "CommunicationWayKey"}},
    ]


def test_free_text_of_other_objects_goes_but_configuration_names_stay():
    scrubbed = Scrubber(keep=RUN_ID)([
        {"objectName": "InvoicePos", "name": "Maths for Synthia Examplova", "text": "Synthia", "quantity": "1.5"},
        {"objectName": "Invoice", "addressParentName": "Synthia Examplova", "sumTax": "0.00"},
        {"objectName": "Unity", "name": "Std."},
        {"objectName": "Part", "name": "Maths"},
        {"objectName": "ContactCustomFieldSetting", "name": "Zahlungsart"},
    ])

    assert scrubbed == [
        {"objectName": "InvoicePos", "name": "[SCRUBBED]", "text": "[SCRUBBED]", "quantity": "1.5"},
        {"objectName": "Invoice", "addressParentName": "[SCRUBBED]", "sumTax": "0.00"},
        {"objectName": "Unity", "name": "Std."},
        {"objectName": "Part", "name": "Maths"},
        {"objectName": "ContactCustomFieldSetting", "name": "Zahlungsart"},
    ]


PATTERNS = {
    "a phone in brackets": "(0170) 1234567",
    "an international phone": "+49 (0) 170 1234567",
    "a phone after a label": "Tel.:+491701234567",
    "a phone with dots": "0170.1234567",
    "a phone with a slash": "030/1234567",
    "a url-encoded e-mail": "pay at https://pay.test/?m=synthia%40mail.example.com",
    "an e-mail in capitals": "SYNTHIA@MAIL.EXAMPLE.COM",
    "an IBAN with no-break spaces": "DE89\u00a03704\u00a00044\u00a00532\u00a00130\u00a000",
    "an IBAN in lower case": "de89 3704 0044 0532 0130 00",
    "a glued token": f"tok{TOKEN}",
    "a short base64 blob": base64.b64encode(b"Synthia Examplova, Example Street 1 " * 8).decode(),
}


def test_every_pattern_is_scrubbed_in_any_string():
    scrub = Scrubber()

    for name, value in PATTERNS.items():
        scrubbed = scrub.text(f"see {value} here")
        assert leaks(scrubbed) == [], name
        assert not any(part in scrubbed for part in ("1234567", "Synthia", "0532", "MAIL.EXAMPLE", TOKEN)), name


def test_dates_amounts_and_the_run_id_are_no_phone_number():
    text = f"period 01.10.2026 to 31.10.2026, sum 1234567.89, run {RUN_ID}, at 2026-11-02T00:00:00+01:00"

    assert Scrubber().text(text) == text


def test_keys_and_the_request_path_are_scrubbed_too():
    exchange = replace(
        _exchange(),
        url="https://my.sevdesk.de/api/v1/Contact/Factory/byEmail/synthia@mail.example.com",
        request_body={"synthia@mail.example.com": 1},
    )

    record = _recorded(exchange)

    assert record["request"]["path"] == "/api/v1/Contact/Factory/byEmail/person@example.org"
    assert record["request"]["body"] == {"person@example.org": 1}


def test_what_a_response_reveals_is_scrubbed_from_its_own_request():
    exchange = replace(
        _exchange(),
        url="https://my.sevdesk.de/api/v1/SevClient?filter=Example+Tutoring+Company+TRIAL",
        request_body={"objectName": "Part", "name": f"Course by {COMPANY}"},
    )

    record = _recorded(exchange)

    assert record["request"]["params"] == {"filter": "[SCRUBBED]"}
    assert record["request"]["body"] == {"objectName": "Part", "name": "Course by [SCRUBBED]"}


async def test_an_unarmed_recorder_writes_nothing(tmp_path: Path):
    recorder = Recorder(tmp_path / "sevdesk")

    recorder(_exchange())

    assert not (tmp_path / "sevdesk").exists()
    recorder.arm()
    recorder(_exchange())
    assert [path.name for path in (tmp_path / "sevdesk").iterdir()] == ["exchange-01.json"]
