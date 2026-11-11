"""Read and write models of the billing API.

Read models are built by explicit ``from_model`` mappers. Update models use Pydantic's experimental ``MISSING``
sentinel, as the CRM's do: an unset field is absent from ``model_dump()``, so the service sees its default.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Self

from pydantic import AfterValidator, AwareDatetime, Field, StringConstraints
from pydantic.experimental.missing_sentinel import MISSING

from app.api.v1.common import ApiModel
from app.core.db.models import Lesson, LessonStatus
from app.services.crm.inputs import require_storable_text

# A lesson longer than a day is a typo. Rule G (minutes divisible by 15) is request validation: the catalog has no
# class for it.
MAX_LESSON_MINUTES = 24 * 60
# A Postgres INTEGER; a larger value could not be bound. Rule G for a rate (divisible by 4) is the service's, so a
# lesson's own rate answers `rate_not_quarter_hour` like a student's rate does.
MAX_CENTS_PER_HOUR = 2**31 - 1


# The instants a Python datetime can hold in UTC: one outside could neither be stored in UTC nor read back.
_FIRST_INSTANT = datetime.min.replace(tzinfo=UTC)
_LAST_INSTANT = datetime.max.replace(tzinfo=UTC)


def _in_utc(value: datetime) -> datetime:
    """Return the instant in UTC, as it is stored."""
    if not _FIRST_INSTANT <= value <= _LAST_INSTANT:
        raise ValueError("Value is out of range")
    return value.astimezone(UTC)


# Plain assignments inline the constraints at the field; a PEP 695 alias would become its own schema.
SubjectLabel = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    AfterValidator(require_storable_text),
    Field(examples=["Mathe"]),
]
Note = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
    AfterValidator(require_storable_text),
]
StartsAt = Annotated[AwareDatetime, AfterValidator(_in_utc), Field(examples=["2026-11-02T16:00:00+01:00"])]
Minutes = Annotated[int, Field(gt=0, le=MAX_LESSON_MINUTES, multiple_of=15, examples=[60])]
CentsPerHour = Annotated[int, Field(ge=0, le=MAX_CENTS_PER_HOUR, examples=[3000])]


class LessonResponse(ApiModel):
    """One appointment of one student."""

    id: uuid.UUID
    """ID of the lesson."""
    student_id: uuid.UUID
    """ID of the student's party."""
    subject: str
    """The subject as a label (`Mathe`), as the calendar title names it."""
    starts_at: datetime
    """When the lesson starts, in UTC. Lessons are billed by its day in `Europe/Berlin`."""
    minutes: int
    """The billed duration, a multiple of 15."""
    status: LessonStatus
    """`planned` until the maintainer confirms it: `held` (the tick), `cancelled` (late or not attended) or
    `not_held` (costs nothing)."""
    billable: bool
    """Whether the lesson is billed: set by the tick, switched off on a `held` lesson as goodwill or on for a
    `cancelled` one; never on a `planned` or `not_held` lesson."""
    rate_cents_per_hour: int | None
    """The lesson's own rate in cents per hour, or `null` for the student's rate or the default."""
    note: str | None
    """Free-text note."""
    created_at: datetime
    """When the lesson was recorded."""
    updated_at: datetime
    """When the lesson last changed."""

    @classmethod
    def from_model(cls, lesson: Lesson) -> Self:
        return cls(
            id=lesson.id,
            student_id=lesson.student_party_id,
            subject=lesson.subject,
            starts_at=lesson.starts_at,
            minutes=lesson.minutes,
            status=lesson.status,
            billable=lesson.billable,
            rate_cents_per_hour=lesson.rate_cents_per_hour,
            note=lesson.note,
            created_at=lesson.created_at,
            updated_at=lesson.updated_at,
        )


class LessonCreateRequest(ApiModel):
    """Body of `POST /lessons`: a lesson added by hand. It starts `planned` and not billable."""

    student_id: uuid.UUID
    """ID of a party holding the student role."""
    subject: SubjectLabel
    """The subject as a label (`Mathe`). Surrounding whitespace is stripped."""
    starts_at: StartsAt
    """When the lesson starts, with its UTC offset (`Z` or `+01:00`)."""
    minutes: Minutes
    """The duration, a multiple of 15."""
    rate_cents_per_hour: CentsPerHour | None = None
    """The lesson's own rate in cents per hour: 0 or more, divisible by 4. Omit it for the student's rate or the
    default."""
    note: Note | None = None
    """Free-text note."""


class LessonUpdateRequest(ApiModel):
    """Body of `PATCH /lessons/{lesson_id}`: only the fields that are sent change; an empty body changes nothing.

    Changing `status` sets the default `billable` of the new status: `held` (the tick) makes the lesson billable,
    every other status does not. Sending the status the lesson already has changes nothing. A `billable` sent along
    wins; `true` is `lesson_not_billable` for a `planned` or `not_held` lesson.
    """

    status: LessonStatus | MISSING = MISSING
    """The new status; `held` is the tick."""
    billable: bool | MISSING = MISSING
    """Whether the lesson is billed: `false` on a `held` lesson is goodwill, `true` on a `cancelled` one bills it."""
    student_id: uuid.UUID | MISSING = MISSING
    """ID of another party holding the student role."""
    subject: SubjectLabel | MISSING = MISSING
    """The subject as a label. Surrounding whitespace is stripped."""
    starts_at: StartsAt | MISSING = MISSING
    """When the lesson starts, with its UTC offset."""
    minutes: Minutes | MISSING = MISSING
    """The duration, a multiple of 15 - corrected when a lesson ran differently."""
    rate_cents_per_hour: CentsPerHour | None | MISSING = MISSING
    """The lesson's own rate in cents per hour, divisible by 4; `null` falls back to the student's rate or the
    default."""
    note: Note | None | MISSING = MISSING
    """Free-text note; `null` clears it."""
