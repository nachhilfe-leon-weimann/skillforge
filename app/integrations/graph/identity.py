"""The identity check: no calendar is read before the configured calendar is confirmed in the configured mailbox.

The app holds Exchange RBAC for one mailbox only, no directory permission, so the mailbox is confirmed through its
calendars: the one named ``GRAPH__CALENDAR_NAME`` must exist once and be owned by ``GRAPH__MAILBOX``. It fails
closed - on a calendar read that fails, a missing or ambiguous calendar, a missing owner and another owner. Its
messages name the settings, never a mailbox, a calendar name or an id.
"""

from collections.abc import Awaitable, Callable

from app.integrations.errors import IdentityCheckError, IntegrationError

from .models import Calendar


async def verify_calendar(read_calendars: Callable[[], Awaitable[list[Calendar]]], *, mailbox: str, name: str) -> str:
    """The id of the lesson calendar, once it is confirmed to be ``name`` in ``mailbox``."""
    try:
        calendars = await read_calendars()
    except IntegrationError as exc:
        # No chaining: the cause may carry the remote system's message.
        raise IdentityCheckError(
            f"the calendars of GRAPH__MAILBOX could not be read ({type(exc).__name__})", status=exc.status
        ) from None
    matches = [calendar for calendar in calendars if calendar.name == name]
    if len(matches) != 1:
        raise IdentityCheckError(f"GRAPH__CALENDAR_NAME names {len(matches)} calendars of GRAPH__MAILBOX, not one")
    owner = matches[0].owner
    if owner is None or not owner.address:
        raise IdentityCheckError("the calendar named GRAPH__CALENDAR_NAME has no owner")
    if owner.address.casefold() != mailbox.casefold():
        raise IdentityCheckError(
            "the calendar named GRAPH__CALENDAR_NAME belongs to another mailbox than GRAPH__MAILBOX"
        )
    return matches[0].id
