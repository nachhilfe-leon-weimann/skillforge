"""Graph's calendars and events as SkillForge reads them.

The models declare only what the import uses and ignore the rest. Times arrive in UTC (the client asks for it)
with seven fractional digits; ``originalStart`` may be missing or ``null``, and an empty subject or location may be
``null`` too - each reads as its default. A name, a subject and a location stay out of ``repr``.
"""

from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field
from pydantic.alias_generators import to_camel


class GraphModel(BaseModel):
    # A title holds a name: no validation error repeats an input, whether or not a caller chains the error.
    model_config = ConfigDict(
        alias_generator=to_camel, validate_by_name=True, extra="ignore", frozen=True, hide_input_in_errors=True
    )


def _utc(value: object) -> object:
    """A ``dateTimeTimeZone`` in UTC as an ISO instant; any other time zone is refused."""
    match value:
        case {"dateTime": str(moment), "timeZone": "UTC"}:
            return f"{moment}Z"
    raise ValueError("a time must be a dateTimeTimeZone in UTC")


def _or_empty(value: object) -> object:
    return "" if value is None else value


def _display_name(value: object) -> object:
    match value:
        case None:
            return ""
        case {"displayName": name}:
            return _or_empty(name)
        case {}:
            return ""
    return value


type UtcTime = Annotated[AwareDatetime, BeforeValidator(_utc)]
type Text = Annotated[str, BeforeValidator(_or_empty)]


class EmailAddress(GraphModel):
    """An ``emailAddress``: only its address is read, never its name."""

    address: str | None = Field(default=None, repr=False)


class Calendar(GraphModel):
    id: str = Field(repr=False)
    name: str = Field(repr=False)
    owner: EmailAddress | None = None


class EventType(StrEnum):
    SINGLE_INSTANCE = "singleInstance"
    OCCURRENCE = "occurrence"
    EXCEPTION = "exception"
    SERIES_MASTER = "seriesMaster"


class CalendarEvent(GraphModel):
    """One occurrence of ``calendarView``: what the import needs of it."""

    id: str
    type: EventType
    series_master_id: str | None = None
    subject: Text = Field(default="", repr=False)
    """The title: ``<subject> mit <name>`` for a lesson."""
    start: UtcTime
    end: UtcTime
    original_start: AwareDatetime | None = None
    """The planned start of an occurrence of a series; missing or ``None`` for a single instance."""
    is_all_day: bool = False
    is_cancelled: bool = False
    is_online_meeting: bool = False
    categories: Annotated[tuple[str, ...], BeforeValidator(lambda value: () if value is None else value)] = ()
    location: Annotated[str, BeforeValidator(_display_name)] = Field(default="", repr=False)
    """The location's display name."""
