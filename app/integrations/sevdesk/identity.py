"""The identity check: no call but the identity read goes to a sevDesk account that is not the configured one.

It fails closed - on a missing ``SEVDESK__EXPECTED_ACCOUNT_ID``, an account read that fails, another account id,
and for a ``trial`` account a company name without the ``TRIAL`` marker. Its messages carry account ids, never
the company name.
"""

from collections.abc import Awaitable, Callable

from app.integrations.config import IntegrationAccount
from app.integrations.errors import IdentityCheckError, IntegrationError

from .config import SevdeskSettings
from .models import Account

TRIAL_MARKER = "TRIAL"
"""What the company name of a sevDesk trial account must contain."""


async def verify_account(read_account: Callable[[], Awaitable[Account]], settings: SevdeskSettings) -> Account:
    """Read the account the token belongs to and confirm it is the configured one."""
    expected = settings.expected_account_id
    if expected is None:
        raise IdentityCheckError("SEVDESK__EXPECTED_ACCOUNT_ID is not set: the sevDesk account cannot be confirmed")
    try:
        account = await read_account()
    except IntegrationError as exc:
        raise IdentityCheckError("the sevDesk account could not be read") from exc
    if account.id != expected:
        raise IdentityCheckError(f"the token belongs to sevDesk account {account.id}, not to {expected}")
    if settings.account is IntegrationAccount.TRIAL and TRIAL_MARKER not in account.name:
        raise IdentityCheckError(f"sevDesk account {account.id} is configured as trial but lacks {TRIAL_MARKER}")
    return account
