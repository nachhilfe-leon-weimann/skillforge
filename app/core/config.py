import re
from functools import lru_cache
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator, Field, StrictInt
from pydantic_settings import NoDecode, SettingsConfigDict
from skillcore.config import CoreSettings

from app.integrations.graph.config import GraphSettings
from app.integrations.sevdesk.config import SevdeskSettings

from .auth.config import AuthSettings
from .db.config import DatabaseSettings
from .logging import LoggingSettings

_WHOLE_NUMBER = re.compile(r"-?[0-9]+", re.ASCII)
_PLAIN_ITEM = re.compile(r"[A-Za-z0-9._-]+", re.ASCII)


def _whole_number(value: object) -> object:
    """A whole number written as digits - as a variable holds it - as an ``int``; anything else as it is, for
    ``StrictInt`` to refuse: lax parsing would take ``32.00`` or ``32.0`` as 32."""
    return int(value) if isinstance(value, str) and _WHOLE_NUMBER.fullmatch(value) else value


type WholeNumber = Annotated[StrictInt, BeforeValidator(_whole_number)]


def _comma_separated(value: object) -> object:
    """A list written as a variable holds it - ``1000,1001`` - as its stripped, non-empty items; anything else as it
    is. ``NoDecode`` keeps the environment's value a string instead of JSON."""
    if isinstance(value, str):
        return frozenset(item for part in value.split(",") if (item := part.strip()))
    return value


def _plain_items(items: frozenset[str]) -> frozenset[str]:
    """Every item letters, digits, ``.``, ``_`` or ``-``: a list written otherwise - as JSON, quoted, with a space
    inside an item - fails the load instead of matching nothing. The message never repeats an item."""
    if not all(_PLAIN_ITEM.fullmatch(item) for item in items):
        raise ValueError("write the items comma-separated, each of letters, digits, '.', '_' or '-' only")
    return items


type CommaSeparated = Annotated[
    frozenset[str], NoDecode, BeforeValidator(_comma_separated), AfterValidator(_plain_items)
]


class BillingSettings(CoreSettings):
    """Billing settings (``BILLING__*``); every one has a default."""

    default_rate_cents_per_hour: WholeNumber = Field(default=3000, ge=0, multiple_of=4)
    """The rate of a lesson without its own rate and of a student without one (billing spec, decision E). Rule G holds
    here as in the database: cents per hour divisible by 4, so every quarter hour is a whole cent."""
    paypal_blocked_customer_numbers: CommaSeparated = Field(default=frozenset(), repr=False)
    """The sevDesk customer numbers that may not pay by PayPal, comma-separated (billing spec, decision K). Read only
    by the payment-request lookup, compared exactly, and never logged; a malformed list fails the load."""

    # A refused list must not repeat a customer number.
    model_config = SettingsConfigDict(env_prefix="BILLING__", hide_input_in_errors=True)


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
