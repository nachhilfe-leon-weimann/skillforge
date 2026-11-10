from typing import Annotated

from pydantic import AfterValidator, PositiveInt, SecretStr
from pydantic_settings import SettingsConfigDict

from app.integrations.config import EmptyIsDefault, IntegrationSettings

type AccountObjectId = Annotated[PositiveInt | None, EmptyIsDefault]


def _token(token: SecretStr) -> SecretStr:
    value = token.get_secret_value()
    if not value or value != "".join(value.split()):
        raise ValueError("SEVDESK__TOKEN must be a token without whitespace; leave the variable out to unset it")
    return token


class SevdeskSettings(IntegrationSettings):
    """sevDesk settings (``SEVDESK__*``). All optional, so the app starts without them; a call without
    ``token`` or ``expected_account_id`` is refused.

    The ids belong to one sevDesk account and differ between the trial and the live account, so they are
    configuration, never literals in code. An empty id means unset; an empty token is refused, as it is a secret
    that failed to load.
    """

    token: Annotated[SecretStr, AfterValidator(_token)] | None = None
    """The API token, sent as the raw ``Authorization`` header."""
    expected_account_id: AccountObjectId = None
    """The ``SevClient`` id the token must belong to (identity check, fail closed)."""
    contact_person_id: AccountObjectId = None
    """The ``SevUser`` named as contact person on every invoice."""
    hour_unit_id: AccountObjectId = None
    """The ``Unity`` of an invoice position counted in hours."""
    payment_method_field_setting_id: AccountObjectId = None
    """The ``ContactCustomFieldSetting`` that holds a contact's payment method."""

    # A malformed token is still a token: no validation error repeats an input.
    model_config = SettingsConfigDict(env_prefix="SEVDESK__", hide_input_in_errors=True)
