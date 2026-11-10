"""The doctor only reads, prints ids and configuration names, and never a person's data."""

import pytest

from app.cli.sevdesk_doctor import build_parser, run
from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.guards import CONTRACT_RUN_ENV
from app.integrations.sevdesk.config import SevdeskSettings
from tests.integrations.sevdesk.fake_sevdesk import ACCOUNT_ID, TRIAL_COMPANY, FakeSevdesk, objects

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
    fake_sevdesk.on("GET", "/ContactCustomFieldSetting/13", objects([setting]))
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
        "ok   payment method (ContactCustomFieldSetting): 13 (Zahlungsart)",
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


async def test_discover_prints_each_candidate_without_personal_data(settings: SevdeskSettings, configured, capsys):
    unchecked = settings.model_copy(update={"expected_account_id": None})

    code, lines = await _doctor(IntegrationAccount.TRIAL, unchecked, configured, discover=True)

    output = capsys.readouterr().out + "\n".join(lines)
    assert code == 0
    assert lines == [
        f"GET /SevClient: id={ACCOUNT_ID} company='{TRIAL_COMPANY}'",
        f"GET /SevUser: sevClient id={ACCOUNT_ID}",
        "GET /ContactCustomFieldSetting: nothing found",
    ]
    assert not any(value in output for value in PERSONAL[:3])


def test_the_parser_requires_an_account():
    assert build_parser().parse_args(["--account", "trial", "--discover"]).account is IntegrationAccount.TRIAL
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
