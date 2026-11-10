import pytest
from pydantic import SecretStr

from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.guards import CONTRACT_RUN_ENV
from app.integrations.sevdesk.config import SevdeskSettings
from tests.integrations.sevdesk.fake_sevdesk import ACCOUNT_ID, TOKEN, FakeSevdesk


@pytest.fixture
def fake_sevdesk() -> FakeSevdesk:
    return FakeSevdesk()


@pytest.fixture
def settings() -> SevdeskSettings:
    return SevdeskSettings(
        _env_file=None,
        mode=IntegrationMode.DRY_RUN,
        account=IntegrationAccount.TRIAL,
        token=SecretStr(TOKEN),
        expected_account_id=ACCOUNT_ID,
        contact_person_id=11,
        hour_unit_id=12,
        payment_method_field_setting_id=13,
    )


@pytest.fixture
def live_settings(settings: SevdeskSettings, monkeypatch) -> SevdeskSettings:
    """Live mode against the synthetic trial: allowed under pytest only in the contract run."""
    monkeypatch.setenv(CONTRACT_RUN_ENV, "1")
    return settings.model_copy(update={"mode": IntegrationMode.LIVE})
