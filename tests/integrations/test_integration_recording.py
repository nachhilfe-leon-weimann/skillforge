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

    assert record["request"]["params"] == {"value": "person@example.org", "limit": "100"}
    assert record["request"]["headers"] == {"accept": "application/json"}
    assert objects[0] == {"id": "0", "objectName": "SevClient", "name": "[SCRUBBED]"}
    assert objects[1]["sevClient"] == {"id": "0", "objectName": "SevClient"}
    assert (objects[4]["id"], objects[4]["invoiceDate"], objects[4]["sumGross"]) == (
        "501",
        "2026-11-02T00:00:00+01:00",
        "45.00",
    )
    assert objects[4]["footText"] == "[SCRUBBED], IBAN [IBAN], call [PHONE] or [PHONE]"
    assert objects[5]["name"] == "Maths"
    assert objects[6]["content"] == f"[{len(PDF)} characters]"


def test_a_binary_response_is_recorded_by_size():
    pdf = replace(_exchange(), response_content=b"%PDF-1.7", response_headers={})

    assert _recorded(pdf)["response"]["body"] == "[8 bytes ]"


def test_a_value_scrubbed_once_is_scrubbed_in_every_later_exchange():
    scrub = Scrubber()
    scrub({"objectName": "SevClient", "name": COMPANY})

    assert scrub({"footText": f"Thanks, {COMPANY}"}) == {"footText": "Thanks, [SCRUBBED]"}


async def test_the_recorder_writes_one_fixture_per_exchange(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SKILLFORGE_CONTRACT_RUN", "1")
    recorder = Recorder(tmp_path / "sevdesk")
    recorder.step = "version"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"objects": {"version": "2.0"}})

    async with IntegrationHttp(
        name="sevdesk",
        base_url="https://my.sevdesk.de/api/v1",
        allowed_hosts=frozenset({"my.sevdesk.de"}),
        headers={"Authorization": TOKEN},
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
