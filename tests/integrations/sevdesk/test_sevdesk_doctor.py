"""The doctor only reads, prints ids and configuration names, and never a person's data."""

import sys
from collections.abc import Mapping

import httpx
import pytest

from app.cli.sevdesk_doctor import build_parser, main, run
from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.guards import CONTRACT_RUN_ENV
from app.integrations.sevdesk.config import SevdeskSettings
from tests.integrations.sevdesk.fake_sevdesk import ACCOUNT_ID, TRIAL_COMPANY, FakeSevdesk, objects, page

PERSONAL = ("Synthia", "Examplova", "synthia@mail.example.com", TRIAL_COMPANY)


async def _doctor(account, settings, fake: FakeSevdesk, *, discover: bool = False) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = await run(account, discover_accounts=discover, settings=settings, transport=fake.transport, out=lines.append)
    return code, lines


@pytest.fixture
def configured(fake_sevdesk: FakeSevdesk) -> FakeSevdesk:
    user = {
        "id": "11",
        "objectName": "SevUser",
        "firstName": "Synthia",
        "lastName": "Examplova",
        "email": "synthia@mail.example.com",
        "sevClient": {"id": str(ACCOUNT_ID), "objectName": "SevClient"},
    }
    fake_sevdesk.on("GET", "/Tools/bookkeepingSystemVersion", objects({"version": "2.0"}))
    fake_sevdesk.on("GET", "/SevUser/11", objects([user]))
    fake_sevdesk.on("GET", "/SevUser", objects([user]))
    fake_sevdesk.on("GET", "/Unity/12", objects([{"id": "12", "objectName": "Unity", "name": "Std."}]))
    setting = {"id": "13", "objectName": "ContactCustomFieldSetting", "name": "Zahlungsart"}
    fake_sevdesk.on("GET", "/ContactCustomFieldSetting", objects([setting]))
    return fake_sevdesk


async def test_a_complete_configuration_passes_without_printing_personal_data(settings, configured, capsys):
    code, lines = await _doctor(IntegrationAccount.TRIAL, settings, configured)

    output = capsys.readouterr().out + "\n".join(lines)
    assert code == 0
    assert lines == [
        f"ok   account: {ACCOUNT_ID} (trial)",
        "ok   bookkeeping system: 2.0",
        "ok   contact person (SevUser): 11",
        "ok   hour unit (Unity): 12 (Std.)",
    ]
    assert not any(value in output for value in PERSONAL)
    assert configured.sent("GET") == configured.sent()


async def test_it_runs_in_dry_run_whatever_the_configured_mode(settings, configured, monkeypatch):
    monkeypatch.delenv(CONTRACT_RUN_ENV, raising=False)
    live = settings.model_copy(update={"mode": IntegrationMode.LIVE})

    assert (await _doctor(IntegrationAccount.TRIAL, live, configured))[0] == 0


@pytest.mark.parametrize(
    ("change", "failure"),
    [
        ({"hour_unit_id": 99}, "FAIL hour unit (Unity): 99 does not exist"),
        ({"contact_person_id": None}, "FAIL contact person (SevUser): SEVDESK__CONTACT_PERSON_ID is not set"),
    ],
)
async def test_a_missing_or_unset_id_fails(settings, configured, change, failure):
    settings = settings.model_copy(update=change)

    code, lines = await _doctor(IntegrationAccount.TRIAL, settings, configured)

    assert code == 1
    assert failure in lines


async def test_a_bookkeeping_system_other_than_2_fails(settings, configured):
    configured.on("GET", "/Tools/bookkeepingSystemVersion", objects({"version": "1.0"}))

    code, lines = await _doctor(IntegrationAccount.TRIAL, settings, configured)

    assert code == 1
    assert "FAIL bookkeeping system: 1.0" in lines


async def test_a_failed_identity_check_stops_after_the_identity_read(settings, configured):
    configured.on("GET", "/SevClient", objects([{"id": "4712", "name": TRIAL_COMPANY}]))

    code, lines = await _doctor(IntegrationAccount.TRIAL, settings, configured)

    assert code == 1
    assert lines == [f"FAIL account: the token belongs to sevDesk account 4712, not to {ACCOUNT_ID}"]
    assert configured.sent() == ["GET /SevClient"]


async def test_an_account_other_than_the_requested_one_is_refused_before_any_call(settings, configured):
    code, lines = await _doctor(IntegrationAccount.LIVE, settings, configured)

    assert code == 1
    assert lines == ["SEVDESK__ACCOUNT is trial, not live: refusing"]
    assert configured.requests == []


DISCOVERED = {
    "a trial": (
        objects([{"id": str(ACCOUNT_ID), "name": TRIAL_COMPANY}]),
        f"id={ACCOUNT_ID} TRIAL marker: yes",
        {"id": "4713", "objectName": "SevClient"},
        "sevClient ids 4713",
    ),
    "no trial": (
        objects([{"id": "4712", "name": "Example Tutoring Company"}]),
        "id=4712 TRIAL marker: no",
        None,
        "sevClient ids none",
    ),
}


@pytest.mark.parametrize(("answer", "expected", "owner", "owners"), DISCOVERED.values(), ids=DISCOVERED.keys())
async def test_discover_sends_its_three_reads_and_prints_ids_and_the_marker_only(
    answer, expected: str, owner: dict[str, str] | None, owners: str, settings: SevdeskSettings, configured, capsys
):
    configured.on("GET", "/SevClient", answer)
    setting = {"id": "13", "objectName": "ContactCustomFieldSetting", "name": "Zahlungsart", "sevClient": owner}
    configured.on("GET", "/ContactCustomFieldSetting", objects([setting]))
    unchecked = settings.model_copy(update={"expected_account_id": None})

    code, lines = await _doctor(IntegrationAccount.TRIAL, unchecked, configured, discover=True)

    output = capsys.readouterr().out + "\n".join(lines)
    assert code == 0
    assert lines == [
        f"ok   GET /SevClient: {expected}",
        f"ok   GET /SevUser: sevClient ids {ACCOUNT_ID}",
        f"ok   GET /ContactCustomFieldSetting: {owners}",
    ]
    assert "Example" not in output and "Zahlungsart" not in output
    assert not any(value in output for value in PERSONAL)
    assert configured.sent() == ["GET /SevClient", "GET /SevUser", "GET /ContactCustomFieldSetting"]


@pytest.mark.parametrize("failing", ["/SevClient", "/SevUser", "/ContactCustomFieldSetting"])
async def test_a_failed_discover_read_fails_the_run_and_stops(failing: str, settings, configured):
    configured.on("GET", failing, httpx.Response(401, json={"error": {"message": "Synthetic"}}))
    unchecked = settings.model_copy(update={"expected_account_id": None})

    code, lines = await _doctor(IntegrationAccount.TRIAL, unchecked, configured, discover=True)

    assert code == 1
    assert len(lines) == 1 and lines[0].startswith(f"FAIL discover: sevdesk GET /api/v1{failing} was rejected")
    reads = ["GET /SevClient", "GET /SevUser", "GET /ContactCustomFieldSetting"]
    assert configured.sent() == reads[: reads.index(f"GET {failing}") + 1]


def test_the_parser_requires_an_account_and_takes_no_abbreviation():
    assert build_parser().parse_args(["--account", "trial", "--discover"]).account is IntegrationAccount.TRIAL
    for arguments in ([], ["--acc", "trial"], ["--account", "trial", "--disc"]):
        with pytest.raises(SystemExit):
            build_parser().parse_args(arguments)


def test_main_refuses_to_run_in_ci(monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(sys, "argv", ["sevdesk_doctor", "--account", "trial"])

    with pytest.raises(SystemExit, match="never runs in CI"):
        main()


# --- --invoice-number: Q19, the maintainer's check of the invoiceNumber filter ---


def _listed(*invoices: Mapping[str, object]) -> httpx.Response:
    return page(list(invoices))


async def _check_numbers(settings, fake: FakeSevdesk, *numbers: str) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = await run(
        IntegrationAccount.TRIAL,
        discover_accounts=False,
        invoice_numbers=numbers,
        settings=settings,
        transport=fake.transport,
        out=lines.append,
    )
    return code, lines


# A synthetic answer that carries what must never be printed: a name, a contact, a customer number, amounts.
SYNTHETIC_INVOICE: dict[str, object] = {
    "id": "73915864",
    "objectName": "Invoice",
    "invoiceNumber": "RE-1000",
    "invoiceType": "RE",
    "status": "200",
    "contact": {"id": "84026975", "objectName": "Contact"},
    "addressName": "Synthia Examplova",
    "customerNumber": "KD-62817",
    "sumGross": "123.45",
    "paidAmount": 67.89,
}


async def test_invoice_numbers_send_only_the_identity_and_the_filtered_reads_and_print_no_personal_data(
    settings, fake_sevdesk: FakeSevdesk, capsys
):
    answers = {
        "RE-1000": _listed(SYNTHETIC_INVOICE),
        "RE-9999": _listed(),
        "RE-100": _listed(
            {**SYNTHETIC_INVOICE, "invoiceType": "SR", "status": "1000"},
            {**SYNTHETIC_INVOICE, "id": "73915865", "invoiceNumber": "RE-100", "status": "100"},
        ),
    }
    fake_sevdesk.on("GET", "/Invoice", lambda request: answers[request.url.params["invoiceNumber"]])

    code, lines = await _check_numbers(settings, fake_sevdesk, "RE-1000", "RE-9999", "RE-100")

    assert lines == [
        f"ok   account: {ACCOUNT_ID} (trial)",
        "ok   invoice number RE-1000: 1 listed - exact match yes, type RE, status 200",
        "ok   invoice number RE-9999: 0 listed",
        "FAIL invoice number RE-100: 2 listed - exact match no, type SR, status 1000;"
        " exact match yes, type RE, status 100",
    ]
    assert code == 1
    assert fake_sevdesk.sent() == ["GET /SevClient", "GET /Invoice", "GET /Invoice", "GET /Invoice"]
    filters = [request.url.params["invoiceNumber"] for request in fake_sevdesk.requests[1:]]
    assert filters == ["RE-1000", "RE-9999", "RE-100"]
    output = capsys.readouterr().out + "\n".join(lines)
    for value in (*PERSONAL, "Synthia", "Examplova", "73915864", "84026975", "KD-62817", "123.45", "67.89"):
        assert value not in output
    assert not any(amount in line for line in lines for amount in ("123", "12345", "67", "6789"))


async def test_every_number_matching_exactly_passes(settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/Invoice", _listed(SYNTHETIC_INVOICE))

    code, lines = await _check_numbers(settings, fake_sevdesk, "RE-1000")

    assert code == 0
    assert lines[-1] == "ok   invoice number RE-1000: 1 listed - exact match yes, type RE, status 200"


async def test_a_failed_listing_fails_the_run_and_prints_no_remote_text(settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/Invoice", httpx.Response(400, json={"error": {"message": "Synthia Examplova"}}))

    code, lines = await _check_numbers(settings, fake_sevdesk, "RE-1000")

    assert code == 1
    assert lines[-1].startswith("FAIL invoice number RE-1000: sevdesk GET /api/v1/Invoice was rejected")
    assert "Synthia" not in "\n".join(lines)


async def test_invoice_numbers_stop_at_a_failed_identity_check(settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/SevClient", objects([{"id": "4712", "name": TRIAL_COMPANY}]))

    code, lines = await _check_numbers(settings, fake_sevdesk, "RE-1000")

    assert code == 1
    assert lines == [f"FAIL account: the token belongs to sevDesk account 4712, not to {ACCOUNT_ID}"]
    assert fake_sevdesk.sent() == ["GET /SevClient"]


def test_the_parser_takes_invoice_numbers_repeatedly_on_the_pattern_and_never_with_discover():
    parsed = build_parser().parse_args(["--account", "live", "--invoice-number", "RE-1", "--invoice-number", "RE-2"])

    assert parsed.invoice_numbers == ["RE-1", "RE-2"]
    assert build_parser().parse_args(["--account", "live"]).invoice_numbers == []
    for arguments in (
        ["--account", "live", "--invoice-number", "re-1"],
        ["--account", "live", "--invoice-number", "RE/1"],
        ["--account", "live", "--invoice-number"],
        ["--account", "live", "--discover", "--invoice-number", "RE-1"],
        ["--account", "live", "--invoice", "RE-1"],
    ):
        with pytest.raises(SystemExit):
            build_parser().parse_args(arguments)
