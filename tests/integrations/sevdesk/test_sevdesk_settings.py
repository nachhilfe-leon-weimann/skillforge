"""sevDesk settings are optional, nested under ``SEVDESK__`` and keep the token out of every representation."""

import re
from pathlib import Path

import pytest

from app.core.config import Settings
from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.sevdesk.config import SevdeskSettings

FIELDS = ("MODE", "ACCOUNT", "TOKEN", "EXPECTED_ACCOUNT_ID", "CONTACT_PERSON_ID", "HOUR_UNIT_ID")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No ``SEVDESK__*`` variable, and a working directory without an env file: independent of the checkout."""
    for field in (*FIELDS, "PAYMENT_METHOD_FIELD_SETTING_ID"):
        monkeypatch.delenv(f"SEVDESK__{field}", raising=False)
        monkeypatch.delenv(f"sevdesk__{field.lower()}", raising=False)
    monkeypatch.chdir(tmp_path)
    return monkeypatch


def test_nested_settings_read_no_env_file_when_the_app_settings_read_none(clean_env, tmp_path):
    (tmp_path / ".env").write_text("SEVDESK__ACCOUNT=live\nSEVDESK__TOKEN=synthetic-from-a-file\n")

    unread = Settings(_env_file=None).sevdesk
    read = Settings().sevdesk

    assert (unread.account, unread.token) == (IntegrationAccount.TRIAL, None)
    assert read.account is IntegrationAccount.LIVE and read.token is not None


def test_the_app_settings_load_without_any_sevdesk_variable(clean_env):
    sevdesk = Settings(_env_file=None).sevdesk

    assert (sevdesk.mode, sevdesk.account) == (IntegrationMode.DRY_RUN, IntegrationAccount.TRIAL)
    assert sevdesk.token is None and sevdesk.expected_account_id is None


def test_the_variables_are_read_under_their_prefix(clean_env):
    for field, value in zip(FIELDS, ("live", "live", "synthetic-token", "4711", "11", "12"), strict=True):
        clean_env.setenv(f"SEVDESK__{field}", value)
    clean_env.setenv("SEVDESK__PAYMENT_METHOD_FIELD_SETTING_ID", "13")

    sevdesk = Settings(_env_file=None).sevdesk

    assert (sevdesk.mode, sevdesk.account) == (IntegrationMode.LIVE, IntegrationAccount.LIVE)
    assert sevdesk.token is not None and sevdesk.token.get_secret_value() == "synthetic-token"
    assert (sevdesk.expected_account_id, sevdesk.contact_person_id, sevdesk.hour_unit_id) == (4711, 11, 12)
    assert sevdesk.payment_method_field_setting_id == 13
    assert "synthetic-token" not in repr(sevdesk) and "synthetic-token" not in str(sevdesk)


@pytest.mark.parametrize(("field", "value"), [("MODE", "write"), ("ACCOUNT", "prod"), ("HOUR_UNIT_ID", "0")])
def test_an_invalid_value_is_refused(clean_env, field: str, value: str):
    clean_env.setenv(f"SEVDESK__{field}", value)

    with pytest.raises(ValueError):
        SevdeskSettings(_env_file=None)


def test_an_empty_variable_means_unset(clean_env):
    for field in (*FIELDS, "PAYMENT_METHOD_FIELD_SETTING_ID"):
        if field != "TOKEN":
            clean_env.setenv(f"SEVDESK__{field}", "")

    for sevdesk in (SevdeskSettings(_env_file=None), Settings(_env_file=None).sevdesk):
        assert (sevdesk.mode, sevdesk.account) == (IntegrationMode.DRY_RUN, IntegrationAccount.TRIAL)
        assert (sevdesk.expected_account_id, sevdesk.contact_person_id, sevdesk.hour_unit_id) == (None, None, None)
        assert sevdesk.payment_method_field_setting_id is None


@pytest.mark.parametrize(
    "token", ["", " ", "synthetic token", "synthetic-token\n", "synthetic\ttoken", "synthetic-tökén", "synthetic\x7f"]
)
def test_an_empty_or_spaced_token_is_refused(clean_env, token: str):
    clean_env.setenv("SEVDESK__TOKEN", token)

    for load in (lambda: SevdeskSettings(_env_file=None), lambda: Settings(_env_file=None)):
        with pytest.raises(ValueError, match="SEVDESK__TOKEN") as raised:
            load()
        assert token.strip() == "" or token not in str(raised.value)


def test_the_example_env_file_lists_every_setting_and_nothing_else():
    example = (Path(__file__).resolve().parents[3] / ".env.example").read_text()
    listed = {name.lower() for name in re.findall(r"^#?SEVDESK__(\w+)=$", example, flags=re.MULTILINE)}

    assert listed == set(SevdeskSettings.model_fields)
    assert re.search(r"^SEVDESK__TOKEN=", example, flags=re.MULTILINE) is None, "an empty token is refused"
