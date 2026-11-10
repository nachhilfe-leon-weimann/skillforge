from functools import lru_cache

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from skillcore.config import CoreSettings

from app.integrations.sevdesk.config import SevdeskSettings

from .auth.config import AuthSettings
from .db.config import DatabaseSettings
from .logging import LoggingSettings


class Settings(CoreSettings):
    # The settings hold secrets (the database URL, the sevDesk token): no validation error repeats an input.
    model_config = SettingsConfigDict(hide_input_in_errors=True)

    auth: AuthSettings
    db: DatabaseSettings
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    sevdesk: SevdeskSettings = Field(default_factory=SevdeskSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
