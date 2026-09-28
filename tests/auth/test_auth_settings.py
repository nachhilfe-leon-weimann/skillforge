import pytest
from pydantic import ValidationError

from app.core.auth import AuthSettings


@pytest.mark.parametrize(
    "setting", ["invitation_expire_hours", "password_reset_expire_hours", "discord_link_code_expire_hours"]
)
def test_an_action_token_lifetime_must_be_positive(setting: str):
    """A lifetime of zero hours would issue tokens that are expired on arrival."""
    with pytest.raises(ValidationError):
        AuthSettings.model_validate({"secret_key": "test-signing-secret-with-at-least-32-bytes", setting: 0})
