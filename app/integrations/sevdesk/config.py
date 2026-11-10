from pydantic import PositiveInt, SecretStr
from pydantic_settings import SettingsConfigDict

from app.integrations.config import IntegrationSettings


class SevdeskSettings(IntegrationSettings):
    """sevDesk settings (``SEVDESK__*``). All optional, so the app starts without them; a call without
    ``token`` or ``expected_account_id`` is refused.

    The ids belong to one sevDesk account and differ between the trial and the live account, so they are
    configuration, never literals in code.
    """

    token: SecretStr | None = None
    """The API token, sent as the raw ``Authorization`` header."""
    expected_account_id: PositiveInt | None = None
    """The ``SevClient`` id the token must belong to (identity check, fail closed)."""
    contact_person_id: PositiveInt | None = None
    """The ``SevUser`` named as contact person on every invoice."""
    hour_unit_id: PositiveInt | None = None
    """The ``Unity`` of an invoice position counted in hours."""
    payment_method_field_setting_id: PositiveInt | None = None
    """The ``ContactCustomFieldSetting`` that holds a contact's payment method."""

    model_config = SettingsConfigDict(env_prefix="SEVDESK__")
