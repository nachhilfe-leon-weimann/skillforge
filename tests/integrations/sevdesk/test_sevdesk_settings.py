"""sevDesk settings are optional, nested under ``SEVDESK__`` and keep the token out of every representation."""

import pytest

from app.core.config import Settings
from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.sevdesk.config import SevdeskSettings

FIELDS = ("MODE", "ACCOUNT", "TOKEN", "EXPECTED_ACCOUNT_ID", "CONTACT_PERSON_ID", "HOUR_UNIT_ID")


@pytest.fixture
def clean_env(monkeypatch):
    for field in (*FIELDS, "PAYMENT_METHOD_FIELD_SETTING_ID"):
        monkeypatch.delenv(f"SEVDESK__{field}", raising=False)
    return monkeypatch


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
