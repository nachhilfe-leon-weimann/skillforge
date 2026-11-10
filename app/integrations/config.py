from enum import StrEnum
from typing import Annotated

from pydantic import BeforeValidator
from pydantic_core import PydanticUseDefault
from skillcore.config import CoreSettings


class IntegrationMode(StrEnum):
    """Whether an integration's writes leave the process."""

    DRY_RUN = "dry_run"
    """Reads go out, writes do not: a write returns the request it would have sent."""
    LIVE = "live"
    """Reads and writes go out."""


class IntegrationAccount(StrEnum):
    """Which account of the remote system the settings point at."""

    TRIAL = "trial"
    """A test account holding synthetic data only."""
    LIVE = "live"
    """The production account."""


def _default_if_empty(value: object) -> object:
    if value == "":
        raise PydanticUseDefault
    return value


EmptyIsDefault = BeforeValidator(_default_if_empty)
"""An empty variable (``SEVDESK__HOUR_UNIT_ID=``, as ``.env.example`` lists it) means unset: the default applies."""


class IntegrationSettings(CoreSettings):
    """Base of every integration's settings; a subclass sets its ``env_prefix`` (``SEVDESK__``).

    Everything has a default, so the app starts without any integration configured: the default mode
    is ``dry_run`` and the default account ``trial``, whose identity check refuses a production account.
    """

    mode: Annotated[IntegrationMode, EmptyIsDefault] = IntegrationMode.DRY_RUN
    account: Annotated[IntegrationAccount, EmptyIsDefault] = IntegrationAccount.TRIAL
