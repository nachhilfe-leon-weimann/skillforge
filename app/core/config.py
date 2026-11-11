from functools import lru_cache

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from skillcore.config import CoreSettings

from app.integrations.graph.config import GraphSettings
from app.integrations.sevdesk.config import SevdeskSettings

from .auth.config import AuthSettings
from .db.config import DatabaseSettings
from .logging import LoggingSettings


class BillingSettings(CoreSettings):
    """Billing settings (``BILLING__*``); every one has a default."""

    default_rate_cents_per_hour: int = Field(default=3000, ge=0, multiple_of=4)
    """The rate of a lesson without its own rate and of a student without one (billing spec, decision E). Rule G holds
    here as in the database: cents per hour divisible by 4, so every quarter hour is a whole cent."""

    model_config = SettingsConfigDict(env_prefix="BILLING__")


class Settings(CoreSettings):
    # The settings hold secrets (the database URL, the sevDesk token): no validation error repeats an input.
    model_config = SettingsConfigDict(hide_input_in_errors=True)

    auth: AuthSettings
    db: DatabaseSettings
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    # Nested settings come from this class's sources; the default reads no env file of its own.
    sevdesk: SevdeskSettings = Field(default_factory=lambda: SevdeskSettings(_env_file=None))
    graph: GraphSettings = Field(default_factory=lambda: GraphSettings(_env_file=None))
    billing: BillingSettings = Field(default_factory=lambda: BillingSettings(_env_file=None))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
