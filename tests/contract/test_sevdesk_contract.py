"""Read-only contract tests against the synthetic sevDesk trial account: `just contract-sevdesk`.

Skipped in every other run. The settings come from the trial env file the recipe loads (``.env`` is ignored); the
tests only read, whatever ``SEVDESK__MODE`` says. An expired trial (a 401 on the identity read) skips them.
"""

import pytest

from app.cli.sevdesk_doctor import configured_ids
from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.sevdesk.config import SevdeskSettings
from tests.contract.sevdesk_trial import connect_trial

pytestmark = pytest.mark.contract


@pytest.fixture
def trial() -> SevdeskSettings:
    settings = SevdeskSettings(_env_file=None)
    if settings.account is not IntegrationAccount.TRIAL:
        pytest.fail("the contract run reads the trial account only: set SEVDESK__ACCOUNT=trial")
    return settings.model_copy(update={"mode": IntegrationMode.DRY_RUN})


async def test_the_identity_check_passes_and_every_configured_id_exists(trial: SevdeskSettings):
    async with connect_trial(trial) as client:
        checks = await configured_ids(client, trial)

    assert [str(check) for check in checks if not check.ok] == []


async def test_the_reads_billing_needs_parse(trial: SevdeskSettings):
    async with connect_trial(trial) as client:
        await client.custom_field_settings()
        await client.parts()
        sequence = await client.invoice_sequence()

    assert sequence.next_sequence > 0
