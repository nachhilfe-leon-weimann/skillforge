"""The spike script, against a synthetic sevDesk only: it refuses anything but live on the trial and records it all."""

import json
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.integrations.config import IntegrationAccount, IntegrationMode
from scripts import sevdesk_spike
from scripts.sevdesk_spike import STEPS, TOPUP_TEXT, WRONG_TOKEN, build_parser, run, s24_dates
from tests.integrations.sevdesk.fake_sevdesk import ACCOUNT_ID, TOKEN, TRIAL_COMPANY, FakeSevdesk, objects, page
from tests.integrations.sevdesk.test_sevdesk_recipes import ROOT
from tests.test_fixture_scan import leaks

RUN_ID = "run202611101200000042"
TAG = f"skillforge-spike:{RUN_ID}"
TRANSFER = f"Spike {RUN_ID}: <strong>Zahlungsverfahren:</strong> <em>Überweisung</em>"
INVOICE = {"id": "501", "objectName": "Invoice", "status": "100", "contact": {"id": "21", "objectName": "Contact"}}
PDF = b"%PDF-1.7 synthetic"
HEX_TOKEN = "0123456789abcdef0123456789abcdef"
CONTACT = {"id": "21", "objectName": "Contact", "customerNumber": "1001", "surename": "Spike", "familyname": RUN_ID}
FIELD = {
    "id": "71",
    "objectName": "ContactCustomField",
    "contact": {"id": "21", "objectName": "Contact"},
    "contactCustomFieldSetting": {"id": "13", "objectName": "ContactCustomFieldSetting"},
    "value": "PayPal",
}
OTHER_CONTACTS_FIELD = {**FIELD, "id": "72", "contact": {"id": "22", "objectName": "Contact"}, "value": "Überweisung"}


def _identity(request: httpx.Request) -> httpx.Response:
    if request.headers["authorization"] != TOKEN:
        return httpx.Response(401, json={"error": {"message": "Authentication required"}})
    return objects([{"id": str(ACCOUNT_ID), "objectName": "SevClient", "name": TRIAL_COMPANY}])


def _save_invoice(request: httpx.Request) -> httpx.Response:
    invoice = json.loads(request.content)["invoice"]
    if "contact" not in invoice:
        return httpx.Response(500, json={"error": {"message": "Synthetic validation error"}})
    if max(invoice["deliveryDate"], invoice.get("deliveryDateUntil") or "") > invoice["invoiceDate"]:
        # Decision L: a normal sevDesk invoice refuses a delivery date after its invoice date.
        return httpx.Response(400, json={"error": {"message": "Synthetic: delivery date after the invoice date"}})
    return objects({"invoice": INVOICE, "invoicePos": []}, status=201)


@pytest.fixture
def trial(fake_sevdesk: FakeSevdesk) -> FakeSevdesk:
    routes = {
        ("GET", "/Tools/bookkeepingSystemVersion"): objects({"version": "2.0"}),
        ("GET", "/ContactCustomFieldSetting"): objects([{"id": "13", "name": "Zahlungsart"}]),
        ("GET", "/Unity"): objects([{"id": "12", "objectName": "Unity", "name": "Std."}]),
        ("GET", "/SevUser"): objects([{"id": "11", "objectName": "SevUser", "firstName": "Synthia"}]),
        ("GET", "/Part"): page([{"id": "31", "name": "Maths", "unity": {"id": "12", "objectName": "Unity"}}]),
        ("GET", "/Textparser/fetchDictionaryEntriesByType"): objects([
            {"key": "Invoice", "value": [{"key": "INVOICE_NUMBER", "value": "[%RECHNUNGSNUMMER%]"}]}
        ]),
        ("POST", "/Contact"): objects({"id": "21", "objectName": "Contact", "familyname": RUN_ID}, status=201),
        ("POST", "/Invoice/Factory/saveInvoice"): _save_invoice,
        ("GET", "/SevClient"): _identity,
        ("GET", "/Contact/21"): objects([CONTACT]),
        ("GET", "/Contact"): objects([{"id": "21", "objectName": "Contact", "customerNumber": "1001"}]),
        ("GET", "/ContactCustomFieldSetting/13"): objects([
            {"id": "13", "objectName": "ContactCustomFieldSetting", "name": "Zahlungsverfahren"}
        ]),
        ("POST", "/ContactCustomField"): objects({"id": "71", "objectName": "ContactCustomField"}, status=201),
        ("GET", "/ContactCustomField"): page([FIELD, OTHER_CONTACTS_FIELD]),
        ("GET", "/Contact/Factory/findContactsByCustomFieldValue"): objects([{"id": "21", "objectName": "Contact"}]),
        ("GET", "/Invoice/501"): objects([{**INVOICE, "customerInternalNote": TAG, "footText": f"{TAG} {HEX_TOKEN}"}]),
        ("GET", "/Invoice/501/getPositions"): page([]),
        ("GET", "/Invoice"): page([{**INVOICE, "customerInternalNote": TAG}]),
        ("POST", "/Invoice/501/render"): httpx.Response(200, json={"pdf": None, "docId": "x"}),
        ("GET", "/Invoice/501/getPdf"): httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"}),
        ("DELETE", "/Invoice/501"): httpx.Response(200, json={"objects": [None]}),
        ("GET", "/SevSequence/Factory/getByType"): objects({
            "id": "7",
            "format": "RE-[%NUMBER%]",
            "nextSequence": "1001",
            "type": "RE",
        }),
    }
    for (method, path), answer in routes.items():
        fake_sevdesk.on(method, path, answer)
    return fake_sevdesk


async def _spike(settings, fake: FakeSevdesk, tmp_path: Path, *args: str) -> tuple[int, list[str]]:
    arguments = build_parser().parse_args([
        "--run-id",
        RUN_ID,
        "--record-dir",
        str(tmp_path / "fixtures"),
        "--pdf-dir",
        str(tmp_path / "pdf"),
        *args,
    ])
    lines: list[str] = []
    code = await run(arguments, settings=settings, transport=fake.transport, out=lines.append)
    return code, lines


async def test_a_full_walk_creates_tagged_objects_records_everything_and_saves_the_pdf(live_settings, trial, tmp_path):
    code, lines = await _spike(live_settings, trial, tmp_path)

    assert code == 0, lines
    assert [line for line in lines if line.startswith("==")] == [f"== {step}" for step in STEPS]
    assert Counter(trial.sent("POST") + trial.sent("DELETE")) == {
        "POST /Contact": 3,
        "POST /ContactCustomField": 2,
        "POST /Invoice/Factory/saveInvoice": 16,
        "POST /Invoice/501/render": 1,
        "DELETE /Invoice/501": 1,
    }
    assert trial.sent()[-3:] == ["GET /Invoice/501", "DELETE /Invoice/501", "GET /SevSequence/Factory/getByType"]
    written = {request.url.path: json.loads(request.content) for request in trial.requests if request.method == "POST"}
    assert written["/api/v1/Contact"]["familyname"] == RUN_ID
    drafts = [json.loads(r.content)["invoice"] for r in trial.requests if r.url.path.endswith("/saveInvoice")]
    assert all(draft["customerInternalNote"] in (TAG, f"{TAG}:S20", f"{TAG}:S21") for draft in drafts)
    assert drafts[0]["footText"].startswith(f"Spike {RUN_ID}: <strong>Zahlungsverfahren:</strong> <em>PayPal")
    assert "?re=[%RECHNUNGSNUMMER%]&betrag=" in drafts[0]["footText"]
    assert [d.get("footText", "").split("<br>")[0] for d in drafts].count(TRANSFER) == 1, (
        "one variant with FOOT_TRANSFER"
    )
    assert drafts[0]["headText"].startswith(f"Spike {RUN_ID}: [%VOLLEANREDE%],<br>\n<br>\n")
    assert [d.get("header") for d in drafts].count(f"Spike {RUN_ID}: Rechnung Nr. [%DOKUMENTENNUMMER%]") == 1, (
        "one variant with DOCUMENT_TITLE"
    )
    assert sum("?re=[%DOKUMENTENNUMMER%]&betrag=" in d.get("footText", "") for d in drafts) == 1, (
        "one variant with the document number in the pay link"
    )
    positions = [p for r in trial.requests if r.url.path.endswith("/saveInvoice") for p in _positions(r)]
    assert all(p.get("text") for p in positions if "part" not in p), "every lesson carries its description"
    assert [p.get("text") for p in positions if "part" in p] == [TOPUP_TEXT, None], "the top-up's text; Q5's none"
    assert [p["part"]["id"] for p in positions if "part" in p] == [31, 31], "the part the discovery found"
    assert ["name" in p for p in positions if "part" in p] == [True, False], "a top-up, then a part without a name"
    pdf_request = next(request for request in trial.requests if request.url.path.endswith("/getPdf"))
    assert pdf_request.url.params["preventSendBy"] == "true"
    assert (tmp_path / "pdf" / f"{RUN_ID}-invoice-501.pdf").read_bytes() == PDF
    fixtures = sorted((tmp_path / "fixtures" / RUN_ID).iterdir())
    # Not recorded: the first identity read, and the write whose answer the cut connection dropped.
    assert len(fixtures) == len(trial.requests) - 2
    assert "identity-01.json" in {fixture.name for fixture in fixtures}
    for fixture in fixtures:
        text = fixture.read_text()
        assert TOKEN not in text and HEX_TOKEN not in text and "Synthia" not in text and leaks(text) == []
    assert any(TAG in fixture.read_text() for fixture in fixtures if fixture.name.startswith("read-back")), (
        "the run's own texts stay"
    )
    recorded = [json.loads(fixture.read_text())["request"]["body"] for fixture in fixtures]
    feet = [body["invoice"].get("footText") for body in recorded if isinstance(body, dict) and "invoice" in body]
    assert drafts[0]["footText"] in feet, "the placeholders of the run's own texts survive the scrub"
    assert any("[%RECHNUNGSNUMMER%]" in f.read_text() for f in fixtures if f.name.startswith("discovery"))


def _positions(request: httpx.Request) -> list[dict[str, Any]]:
    return json.loads(request.content).get("invoicePosSave") or []


async def test_a_single_step_runs_alone(live_settings, trial, tmp_path):
    untagged = {**INVOICE, "id": "502", "customerInternalNote": f"{TAG}:S20"}
    trial.on("GET", "/Invoice", page([{**INVOICE, "customerInternalNote": TAG}, untagged]))

    code, lines = await _spike(live_settings, trial, tmp_path, "--step", "read-back", "--invoice-id", "501")

    assert code == 0
    assert lines[-1] == "lookup by customerInternalNote: sevDesk listed 2, 1 carry the tag"
    assert trial.sent() == [
        "GET /SevClient",
        "GET /SevClient",
        "GET /SevUser",
        "GET /Invoice/501",
        "GET /Invoice/501/getPositions",
        "GET /Invoice",
    ]


async def test_a_pdf_answer_that_is_no_pdf_stops_the_step_and_saves_nothing(live_settings, trial, tmp_path):
    trial.on("GET", "/Invoice/501/getPdf", objects({"content": "not a document"}))

    code, lines = await _spike(live_settings, trial, tmp_path, "--step", "pdf", "--invoice-id", "501")

    assert code == 1
    assert lines[-1].startswith("stopped: sevdesk GET /Invoice/{id}/getPdf: the answer is no PDF")
    assert not (tmp_path / "pdf").exists()


NOT_THIS_RUNS_DRAFT = {
    "another run's draft": {**INVOICE, "customerInternalNote": "skillforge-spike:run202611091200000007"},
    "a draft without a tag": INVOICE,
    "a tag that only contains this run's": {**INVOICE, "customerInternalNote": f"{TAG} copy"},
    "an issued invoice of this run": {**INVOICE, "customerInternalNote": TAG, "status": "200"},
}


@pytest.mark.parametrize("found", NOT_THIS_RUNS_DRAFT.values(), ids=NOT_THIS_RUNS_DRAFT.keys())
async def test_delete_reads_first_and_deletes_only_a_draft_of_this_run(found, live_settings, trial, tmp_path):
    trial.on("GET", "/Invoice/501", objects([found]))

    with pytest.raises(SystemExit, match="no draft of this run"):
        await _spike(live_settings, trial, tmp_path, "--step", "delete", "--invoice-id", "501")

    assert trial.sent() == ["GET /SevClient", "GET /SevClient", "GET /SevUser", "GET /Invoice/501"]


async def test_delete_of_an_invoice_sevdesk_does_not_know_deletes_nothing(live_settings, trial, tmp_path):
    trial.on("GET", "/Invoice/501", objects([]))

    with pytest.raises(SystemExit, match="no draft of this run"):
        await _spike(live_settings, trial, tmp_path, "--step", "delete", "--invoice-id", "501")

    assert trial.sent("DELETE") == []


async def test_delete_of_this_runs_draft_reads_it_then_deletes_it(live_settings, trial, tmp_path):
    code, _ = await _spike(live_settings, trial, tmp_path, "--step", "delete", "--invoice-id", "501")

    assert code == 0
    assert trial.sent()[3:] == ["GET /Invoice/501", "DELETE /Invoice/501"]


async def test_a_failure_to_record_stops_the_walk(live_settings, trial, tmp_path):
    # A file of an earlier run with this id: the numbering goes on from one file, so the SevUser read collides.
    run_directory = tmp_path / "fixtures" / RUN_ID
    run_directory.mkdir(parents=True)
    (run_directory / "identity-02.json").write_text("recorded before\n")

    code, lines = await _spike(live_settings, trial, tmp_path)

    assert code == 1
    assert lines[-1] == "stopped: recording failed (FileExistsError) in step identity; the fixtures are incomplete"
    assert not any(line.startswith("==") for line in lines)
    assert trial.sent() == ["GET /SevClient", "GET /SevClient", "GET /SevUser"]
    assert (run_directory / "identity-02.json").read_text() == "recorded before\n"


async def test_a_failure_to_record_a_write_stops_before_the_next_call(live_settings, trial, tmp_path):
    # The numbering goes on from the one file there: identity 02 and 03, the setting 04, the first write 05.
    run_directory = tmp_path / "fixtures" / RUN_ID
    run_directory.mkdir(parents=True)
    (run_directory / "payment-methods-05.json").write_text("recorded before\n")

    code, lines = await _spike(live_settings, trial, tmp_path, "--contact-id", "21", "--step", "payment-methods")

    assert lines[-1] == (
        "stopped: recording failed (FileExistsError) in step payment-methods;"
        f" POST /Contact went out; look for {TAG} in the trial"
    )
    assert code == 1 and trial.sent()[-1] == "POST /Contact" and trial.sent("POST") == ["POST /Contact"]


async def test_a_failure_to_record_a_write_names_its_path_without_ids(live_settings, trial, tmp_path):
    run_directory = tmp_path / "fixtures" / RUN_ID
    run_directory.mkdir(parents=True)
    (run_directory / "pdf-04.json").write_text("recorded before\n")

    code, lines = await _spike(live_settings, trial, tmp_path, "--invoice-id", "501", "--step", "pdf")

    assert code == 1 and lines[-1].endswith(f"; POST /Invoice/{{id}}/render went out; look for {TAG} in the trial")


async def test_a_failure_to_record_a_read_sends_no_write_after_it(live_settings, trial, tmp_path):
    run_directory = tmp_path / "fixtures" / RUN_ID
    run_directory.mkdir(parents=True)
    (run_directory / "payment-methods-04.json").write_text("recorded before\n")

    code, lines = await _spike(live_settings, trial, tmp_path, "--contact-id", "21", "--step", "payment-methods")

    assert code == 1 and lines[-1].startswith("stopped: recording failed (FileExistsError) in step payment-methods")
    assert trial.sent("POST") == []


async def test_a_failure_to_record_a_refused_write_stops_too(live_settings, trial, tmp_path):
    trial.on("POST", "/Contact", httpx.Response(400, json={"error": {"message": "Synthetic refusal"}}))
    run_directory = tmp_path / "fixtures" / RUN_ID
    run_directory.mkdir(parents=True)
    (run_directory / "contact-04.json").write_text("recorded before\n")

    code, lines = await _spike(live_settings, trial, tmp_path, "--step", "contact")

    assert code == 1 and lines[-1].startswith("stopped: recording failed (FileExistsError) in step contact;")


async def test_the_sevuser_names_are_learned_before_a_step_run_on_its_own(live_settings, trial, tmp_path):
    trial.on(
        "GET",
        "/SevSequence/Factory/getByType",
        objects({"id": "7", "format": "Synthia-[%NUMBER%]", "nextSequence": "1", "type": "RE"}),
    )

    code, _ = await _spike(live_settings, trial, tmp_path, "--step", "sequence")

    assert code == 0
    assert all("Synthia" not in fixture.read_text() for fixture in (tmp_path / "fixtures" / RUN_ID).iterdir())


@pytest.mark.parametrize("run_id", ["a", "20261110T120000-ab12", "run1"])
async def test_a_run_id_outside_the_format_is_refused_before_any_call(run_id: str, live_settings, trial, tmp_path):
    arguments = build_parser().parse_args(["--run-id", run_id, "--record-dir", str(tmp_path / "fixtures")])
    lines: list[str] = []

    assert await run(arguments, settings=live_settings, transport=trial.transport, out=lines.append) == 1
    assert lines[0].startswith("--run-id is 'run' and 18 digits")
    assert trial.requests == []


async def test_a_write_with_an_unknown_outcome_is_not_retried_and_stops_the_walk(live_settings, trial, tmp_path):
    trial.on("POST", "/Invoice/Factory/saveInvoice", httpx.ReadTimeout("timed out"))

    code, lines = await _spike(live_settings, trial, tmp_path)

    assert code == 1
    assert trial.sent("POST")[-1] == "POST /Invoice/Factory/saveInvoice"
    assert trial.sent("POST").count("POST /Invoice/Factory/saveInvoice") == 1
    assert TAG in lines[-1] and "not retried" in lines[-1]


@pytest.mark.parametrize(
    "change", [{"mode": IntegrationMode.DRY_RUN}, {"account": IntegrationAccount.LIVE}], ids=["dry_run", "live account"]
)
async def test_it_refuses_anything_but_live_mode_on_the_trial(live_settings, trial, tmp_path, change):
    code, lines = await _spike(live_settings.model_copy(update=change), trial, tmp_path)

    assert code == 1 and "only with SEVDESK__MODE=live and SEVDESK__ACCOUNT=trial" in lines[0]
    assert trial.requests == []


@pytest.mark.parametrize(
    "account",
    [{"id": "4711", "name": "Example Tutoring"}, {"id": "4712", "name": TRIAL_COMPANY}],
    ids=["no trial marker", "another account"],
)
async def test_a_failed_identity_check_stops_the_walk_and_records_nothing(live_settings, trial, tmp_path, account):
    trial.on("GET", "/SevClient", objects([account]))

    code, lines = await _spike(live_settings, trial, tmp_path)

    assert code == 1 and lines[-1].startswith("stopped: ")
    assert trial.sent() == ["GET /SevClient"]
    assert not (tmp_path / "fixtures").exists()


UNREADABLE_WRITES = {
    "a contact answer that is no JSON": ("POST", "/Contact", httpx.Response(201, text="<html>ok</html>")),
    "a contact answer without an id": ("POST", "/Contact", objects({"objectName": "Contact"}, status=201)),
    "a render answer that is no JSON": ("POST", "/Invoice/501/render", httpx.Response(200, text="rendered")),
    "a delete answer that is no JSON": ("DELETE", "/Invoice/501", httpx.Response(200, text="deleted")),
}


@pytest.mark.parametrize(("method", "path", "answer"), UNREADABLE_WRITES.values(), ids=UNREADABLE_WRITES.keys())
async def test_an_unreadable_answer_to_a_write_is_an_unknown_outcome(
    live_settings, trial, tmp_path, method, path, answer
):
    trial.on(method, path, answer)

    code, lines = await _spike(live_settings, trial, tmp_path)

    assert code == 1 and lines[-1].startswith("outcome unknown, not retried") and TAG in lines[-1]
    assert trial.sent(method).count(f"{method} {path}") == 1


async def test_the_pdf_never_lands_in_the_repository(live_settings, trial, tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    monkeypatch.setattr(sevdesk_spike, "REPO_ROOT", repository)
    arguments = build_parser().parse_args([
        "--pdf-dir",
        str(repository / "tests"),
        "--record-dir",
        str(tmp_path / "fixtures"),
    ])
    lines: list[str] = []

    assert await run(arguments, settings=live_settings, transport=trial.transport, out=lines.append) == 1
    assert lines[0].startswith("--pdf-dir must lie outside the repository")
    assert trial.requests == [] and not repository.exists()


def _step(lines: list[str], step: str) -> list[str]:
    start = lines.index(f"== {step}") + 1
    return lines[start : next((i for i, line in enumerate(lines) if i >= start and line.startswith("==")), len(lines))]


async def test_the_manifest_steps_report_what_the_rows_ask(live_settings, trial, tmp_path):
    code, lines = await _spike(live_settings, trial, tmp_path)

    assert code == 0, lines
    assert _step(lines, "contact-reads") == ["contact 21: customer number 1001", "by customer number: ['21']"]
    assert "contact 21 (PayPal): payment method values ['PayPal']" in _step(lines, "payment-methods")
    assert _step(lines, "discovery")[-4:] == [
        "Q18 /Part: total '1', 1 object(s)",
        "part 31: Maths (unit 12)",
        "S11 uses part 31",
        "placeholder Invoice.INVOICE_NUMBER: [%RECHNUNGSNUMMER%]",
    ]
    assert [line.split(":")[0] for line in _step(lines, "invoice-variants")] == [
        "several lessons",
        "several children",
        "the transfer foot text",
        "the document title",
        "the pay link with the document number",
        "a top-up",
        "Q18 /Invoice/{id}/getPositions",
        "a part and no name",
    ]
    assert "rounding: local 26240 cents" in _step(lines, "rounding")[1]
    assert _step(lines, "numbers")[0] == "first draft 501: number sent RE-1001, stored None"
    year = date.today().year - 1
    dates = [line for line in _step(lines, "delivery-dates") if ": sent " in line]
    assert [line.split(", stored")[0] for line in dates] == [
        f"{label}: sent {start} to {until}" for label, (start, until) in s24_dates(date.today()).items()
    ]
    assert dates[-1].startswith(f"the last day of the year: sent {year}-12-31 to None")
    assert _step(lines, "cut-connection")[0].startswith("pending, then unknown: sevdesk POST")
    assert _step(lines, "error-bodies") == [
        "a draft without contact and positions: HTTP 500, 'Synthetic validation error'",
        "a wrong token: HTTP 401, 'Authentication required'",
    ]


async def test_the_numbered_drafts_send_the_numbers_of_the_sequence(live_settings, trial, tmp_path):
    code, _ = await _spike(live_settings, trial, tmp_path, "--contact-id", "21", "--step", "numbers")

    assert code == 0
    sent = [json.loads(r.content)["invoice"] for r in trial.requests if r.url.path.endswith("/saveInvoice")]
    assert [draft["invoiceNumber"] for draft in sent] == ["RE-1001", "RE-1002"]


async def test_the_cut_connection_sends_its_write_once_then_only_reads(live_settings, trial, tmp_path):
    code, lines = await _spike(live_settings, trial, tmp_path, "--contact-id", "21", "--step", "cut-connection")

    assert code == 0, lines
    assert trial.sent()[3:] == ["POST /Invoice/Factory/saveInvoice", "GET /Invoice", "GET /Invoice"]
    reads = [dict(request.url.params) for request in trial.requests[4:]]
    assert reads[0] == {"customerInternalNote": f"{TAG}:S20", "countAll": "true", "limit": "1000"}
    assert reads[1]["contact[id]"] == "21" and reads[1]["status"] == "100"
    assert lines[-1] == "reconciled by reference: []"


async def test_the_cut_connection_is_disarmed_when_the_cut_write_fails_on_its_own(live_settings, trial, tmp_path):
    def save(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["invoice"]["customerInternalNote"].endswith(":S20"):
            raise httpx.ConnectError("synthetic", request=request)
        return _save_invoice(request)

    trial.on("POST", "/Invoice/Factory/saveInvoice", save)
    steps = ("--step", "cut-connection", "--step", "error-bodies")

    code, lines = await _spike(live_settings, trial, tmp_path, "--contact-id", "21", *steps)

    assert code == 0, lines
    assert "a draft without contact and positions: HTTP 500, 'Synthetic validation error'" in lines


async def test_an_error_body_step_whose_write_has_an_unknown_outcome_stops(live_settings, trial, tmp_path):
    trial.on("POST", "/Invoice/Factory/saveInvoice", httpx.ConnectError("synthetic"))

    code, lines = await _spike(live_settings, trial, tmp_path, "--contact-id", "21", "--step", "error-bodies")

    assert code == 1 and lines[-1].startswith("outcome unknown, not retried") and TAG in lines[-1]
    assert trial.sent("GET") == ["GET /SevClient", "GET /SevClient", "GET /SevUser"], "no read with a wrong token"


async def test_a_listing_without_a_total_is_reported_and_the_walk_goes_on(live_settings, trial, tmp_path):
    trial.on("GET", "/Part", objects([{"id": "31", "name": "Maths"}]))

    code, lines = await _spike(live_settings, trial, tmp_path, "--step", "discovery")

    assert code == 0, lines
    assert "Q18 /Part: total None, 1 object(s)" in lines and "S11 uses part 31" in lines


@pytest.mark.parametrize("today", [date(2026, 1, 1), date(2026, 11, 10), date(2027, 3, 28), date(2026, 12, 31)])
def test_the_s24_dates_lie_before_any_invoice_date(today: date):
    dates = s24_dates(today)

    assert all(day < today for start, until in dates.values() for day in (start, until) if day is not None)
    spring = dates["the spring clock change"][0]
    autumn_eve, autumn = dates["a period over the autumn clock change"]
    assert autumn is not None and autumn - autumn_eve == timedelta(days=1)
    assert spring.weekday() == autumn.weekday() == 6, "both clock changes fall on a Sunday"
    assert (spring.month, (spring + timedelta(days=7)).month) == (3, 4), "the last Sunday of March"
    assert (autumn.month, (autumn + timedelta(days=7)).month) == (10, 11), "the last Sunday of October"
    assert dates["the last day of the year"][0] == date(today.year - 1, 12, 31)


async def test_the_cut_connection_drops_one_written_answer_and_never_a_read():
    sent: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request.method)
        return httpx.Response(200, json={})

    cut = sevdesk_spike.CutConnection(httpx.MockTransport(answer))
    async with httpx.AsyncClient(transport=cut, base_url="https://sevdesk.example.org") as client:
        cut.armed = True
        await client.get("/Invoice")
        with pytest.raises(httpx.ReadError):
            await client.post("/Invoice")
        await client.post("/Invoice")

    assert sent == ["GET", "POST", "POST"], "the cut write went out, and only it lost its answer"
    assert not cut.armed


async def test_a_read_after_the_ui_is_never_part_of_the_walk_and_runs_alone(live_settings, trial, tmp_path):
    trial.on("GET", "/Invoice/501", httpx.Response(404, json={"error": {"message": "Not found"}}))
    arguments = ("--contact-id", "21", "--invoice-id", "501", "--step", "deleted-read")

    code, lines = await _spike(live_settings, trial, tmp_path, *arguments)

    assert code == 0
    assert "deleted-read" not in STEPS
    assert lines[-3:] == [
        "GET /Invoice/501 after the deletion: HTTP 404",
        "Q18 /Invoice: total '1', 1 object(s)",
        "the contact's invoices: ['501']; 501 still listed",
    ]
    assert trial.sent("POST") == [] and trial.sent("DELETE") == []


async def test_the_wrong_token_never_reaches_a_fixture(live_settings, trial, tmp_path):
    await _spike(live_settings, trial, tmp_path, "--step", "error-bodies", "--contact-id", "21")

    for fixture in (tmp_path / "fixtures" / RUN_ID).iterdir():
        assert WRONG_TOKEN not in fixture.read_text()


async def test_a_refused_position_without_a_name_is_q5s_answer_and_the_walk_goes_on(live_settings, trial, tmp_path):
    def save(request: httpx.Request) -> httpx.Response:
        if any("name" not in position for position in _positions(request)):
            return httpx.Response(400, json={"error": {"message": "Synthetic: a position needs a name"}})
        return _save_invoice(request)

    trial.on("POST", "/Invoice/Factory/saveInvoice", save)

    code, lines = await _spike(live_settings, trial, tmp_path, "--contact-id", "21", "--part-id", "31")

    assert code == 0, lines
    assert "a part and no name: refused, HTTP 400, 'Synthetic: a position needs a name'" in lines


async def test_a_server_error_on_the_position_without_a_name_stops(live_settings, trial, tmp_path):
    def save(request: httpx.Request) -> httpx.Response:
        if any("name" not in position for position in _positions(request)):
            return httpx.Response(503, json={"error": {"message": "Synthetic outage"}})
        return _save_invoice(request)

    trial.on("POST", "/Invoice/Factory/saveInvoice", save)
    arguments = ("--contact-id", "21", "--part-id", "31", "--step", "invoice-variants")

    code, lines = await _spike(live_settings, trial, tmp_path, *arguments)

    assert code == 1 and lines[-1].startswith("outcome unknown, not retried")


async def test_a_part_id_given_is_kept_over_the_discovered_one(live_settings, trial, tmp_path):
    arguments = ("--contact-id", "21", "--part-id", "99", "--step", "discovery", "--step", "invoice-variants")

    code, lines = await _spike(live_settings, trial, tmp_path, *arguments)

    assert code == 0, lines
    parts = {
        p["part"]["id"]
        for r in trial.requests
        if r.url.path.endswith("/saveInvoice")
        for p in _positions(r)
        if "part" in p
    }
    assert parts == {99} and not any(line.startswith("S11 uses part") for line in lines)


async def test_discovery_prints_only_placeholders_as_they_are(live_settings, trial, tmp_path):
    entries = [{"key": "A", "value": "[%KUNDENNUMMER%]"}, {"key": "B", "value": "Synthia Examplova"}]
    trial.on("GET", "/Textparser/fetchDictionaryEntriesByType", objects([{"key": "Invoice", "value": entries}]))

    code, lines = await _spike(live_settings, trial, tmp_path, "--step", "discovery")

    assert code == 0
    assert lines[-2:] == ["placeholder Invoice.A: [%KUNDENNUMMER%]", "placeholder Invoice.B: <not a placeholder>"]


@pytest.mark.parametrize("too_deep_to_parse", [False, True], ids=["no JSON", "too deep"])
async def test_an_unreadable_listing_stops_with_a_message(too_deep_to_parse, request, live_settings, trial, tmp_path):
    body = request.getfixturevalue("too_deep") if too_deep_to_parse else b"<html>no JSON</html>"
    trial.on("GET", "/Part", httpx.Response(200, content=body))

    code, lines = await _spike(live_settings, trial, tmp_path, "--step", "discovery")

    assert code == 1 and lines[-1] == "stopped: sevdesk GET /Part: the answer is no readable JSON"


def _spec_block(name: str, kind: str = "html") -> str:
    spec = (ROOT / "docs" / "specs" / "billing.md").read_text()
    found = re.search(rf"`{name}`[^\n]*:\n\n```{kind}\n(.*?)\n```", spec, re.S)
    assert found, name
    return found[1]


def test_the_s15_texts_are_the_specs_blocks_line_by_line():
    placeholders = {
        "{Vollanrede}": "[%VOLLEANREDE%]",
        "{Zahlungsziel}": "[%ZAHLUNGSZIEL%]",
        "{Kontaktperson}": "[%KONTAKTPERSON%]",
        "{Zahlungslink}": sevdesk_spike.PAY_LINK,
    }
    texts = {"HEAD_TEXT": "HEAD_TEXT", "FOOT_PAYPAL": "FOOT_TEXT", "FOOT_TRANSFER": "FOOT_TRANSFER"}
    for block, constant in texts.items():
        expected = _spec_block(block)
        for written, placeholder in placeholders.items():
            expected = expected.replace(written, placeholder)
        assert getattr(sevdesk_spike, constant) == "Spike {run_id}: " + expected, constant


def test_the_document_title_and_the_top_up_text_are_the_specs_blocks():
    assert sevdesk_spike.DOCUMENT_TITLE == "Spike {run_id}: " + _spec_block("DOCUMENT_TITLE", "text")
    assert sevdesk_spike.TOPUP_TEXT == _spec_block("TOPUP_POSITION_DESCRIPTION", "text")
