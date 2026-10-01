from pydantic import PositiveInt, SecretStr
from pydantic_settings import SettingsConfigDict
from skillcore.config import CoreSettings


class AuthSettings(CoreSettings):
    """Settings for the authentication module."""

    issuer: str = "skillforge"
    audience: str = "skillforge-api"
    secret_key: SecretStr
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    invitation_expire_hours: PositiveInt = 168
    password_reset_expire_hours: PositiveInt = 24
    discord_link_code_expire_hours: PositiveInt = 24
    refresh_token_expire_days: PositiveInt = 30
    login_lockout_threshold: PositiveInt = 5
    login_lockout_max_minutes: PositiveInt = 15

    model_config = SettingsConfigDict(
        env_prefix="AUTH__",
    )
