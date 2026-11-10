"""The spike script, against a synthetic sevDesk only: it refuses anything but live on the trial and records it all."""

import base64
import json
from pathlib import Path

import httpx
import pytest

from app.integrations.config import IntegrationAccount, IntegrationMode
from scripts import sevdesk_spike
from scripts.sevdesk_spike import STEPS, build_parser, run
from tests.integrations.sevdesk.fake_sevdesk import TOKEN, TRIAL_COMPANY, FakeSevdesk, objects, page
from tests.test_fixture_scan import leaks

RUN_ID = "run202611101200000042"
TAG = f"skillforge-spike:{RUN_ID}"
INVOICE = {"id": "501", "objectName": "Invoice", "status": "100", "contact": {"id": "21", "objectName": "Contact"}}
PDF = b"%PDF-1.7 synthetic"
HEX_TOKEN = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def trial(fake_sevdesk: FakeSevdesk) -> FakeSevdesk:
    routes = {
        ("GET", "/Tools/bookkeepingSystemVersion"): objects({"version": "2.0"}),
        ("GET", "/ContactCustomFieldSetting"): objects([{"id": "13", "name": "Zahlungsart"}]),
        ("GET", "/Unity"): objects([{"id": "12", "objectName": "Unity", "name": "Std."}]),
        ("GET", "/SevUser"): objects([{"id": "11", "objectName": "SevUser", "firstName": "Synthia"}]),
        ("GET", "/Part"): page([{"id": "31", "name": "Maths", "unity": {"id": "12", "objectName": "Unity"}}]),
        ("GET", "/Textparser/fetchDictionaryEntriesByType"): objects([{"key": "[%KUNDENNUMMER%]", "value": []}]),
        ("POST", "/Contact"): objects({"id": "21", "objectName": "Contact", "familyname": RUN_ID}, status=201),
        ("POST", "/Invoice/Factory/saveInvoice"): objects({"invoice": INVOICE, "invoicePos": []}, status=201),
        ("GET", "/Invoice/501"): objects([{**INVOICE, "customerInternalNote": TAG, "footText": f"{TAG} {HEX_TOKEN}"}]),
        ("GET", "/Invoice/501/getPositions"): page([]),
        ("GET", "/Invoice"): page([{**INVOICE, "customerInternalNote": TAG}]),
        ("POST", "/Invoice/501/render"): httpx.Response(200, json={"pdf": None, "docId": "x"}),
        ("GET", "/Invoice/501/getPdf"): objects({
            "filename": "RE-1001.pdf",
            "mimeType": "application/pdf",
            "base64encoded": True,
            "content": base64.b64encode(PDF).decode(),
        }),
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
    assert trial.sent("POST") + trial.sent("DELETE") == [
        "POST /Contact",
        "POST /Invoice/Factory/saveInvoice",
        "POST /Invoice/501/render",
        "DELETE /Invoice/501",
    ]
    assert trial.sent()[-3:] == ["GET /Invoice/501", "DELETE /Invoice/501", "GET /SevSequence/Factory/getByType"]
    contact, invoice = [json.loads(request.content) for request in trial.requests if request.method == "POST"][:2]
    assert contact["familyname"] == RUN_ID
    assert invoice["invoice"]["customerInternalNote"] == TAG
    assert invoice["invoice"]["footText"].startswith(f"Spike {RUN_ID}: ")
    pdf_request = next(request for request in trial.requests if request.url.path.endswith("/getPdf"))
    assert pdf_request.url.params["preventSendBy"] == "true"
    assert (tmp_path / "pdf" / f"{RUN_ID}-invoice-501.pdf").read_bytes() == PDF
    fixtures = sorted((tmp_path / "fixtures" / RUN_ID).iterdir())
    assert len(fixtures) == len(trial.requests) - 1, "the first identity read is not recorded, its re-read is"
    assert "identity-01.json" in {fixture.name for fixture in fixtures}
    for fixture in fixtures:
        text = fixture.read_text()
        assert TOKEN not in text and HEX_TOKEN not in text and "Synthia" not in text and leaks(text) == []
    assert TAG in (tmp_path / "fixtures" / RUN_ID / "read-back-10.json").read_text(), "the run's own texts stay"


async def test_a_single_step_runs_alone(live_settings, trial, tmp_path):
    code, _ = await _spike(live_settings, trial, tmp_path, "--step", "read-back", "--invoice-id", "501")

    assert code == 0
    assert trial.sent() == [
        "GET /SevClient",
        "GET /SevClient",
        "GET /Invoice/501",
        "GET /Invoice/501/getPositions",
        "GET /Invoice",
        "GET /Invoice",
    ]


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

    assert trial.sent() == ["GET /SevClient", "GET /SevClient", "GET /Invoice/501"]


async def test_delete_of_an_invoice_sevdesk_does_not_know_deletes_nothing(live_settings, trial, tmp_path):
    trial.on("GET", "/Invoice/501", objects([]))

    with pytest.raises(SystemExit, match="no draft of this run"):
        await _spike(live_settings, trial, tmp_path, "--step", "delete", "--invoice-id", "501")

    assert trial.sent("DELETE") == []


async def test_delete_of_this_runs_draft_reads_it_then_deletes_it(live_settings, trial, tmp_path):
    code, _ = await _spike(live_settings, trial, tmp_path, "--step", "delete", "--invoice-id", "501")

    assert code == 0
    assert trial.sent()[2:] == ["GET /Invoice/501", "DELETE /Invoice/501"]


async def test_a_failure_to_record_stops_the_walk(live_settings, trial, tmp_path):
    # A file of an earlier run with this id: the numbering goes on from one file, so the identity read collides.
    run_directory = tmp_path / "fixtures" / RUN_ID
    run_directory.mkdir(parents=True)
    (run_directory / "identity-02.json").write_text("recorded before\n")

    code, lines = await _spike(live_settings, trial, tmp_path)

    assert code == 1
    assert lines[-1] == "stopped: recording failed (FileExistsError) in step identity; the fixtures are incomplete"
    assert not any(line.startswith("==") for line in lines)
    assert trial.sent() == ["GET /SevClient", "GET /SevClient"]
    assert (run_directory / "identity-02.json").read_text() == "recorded before\n"


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
    assert trial.sent("POST") == ["POST /Contact", "POST /Invoice/Factory/saveInvoice"]
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
