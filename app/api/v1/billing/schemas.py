"""Read and write models of the billing API.

Read models are built by explicit ``from_model`` mappers. Update models use Pydantic's experimental ``MISSING``
sentinel, as the CRM's do: an unset field is absent from ``model_dump()``, so the service sees its default.
"""

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Self

from pydantic import AfterValidator, AwareDatetime, Field, StringConstraints, model_validator
from pydantic.experimental.missing_sentinel import MISSING

from app.api.v1.common import ApiModel
from app.core.db.models import CalendarAlias, Lesson, LessonStatus, ManualPayer, Rate, SevdeskContact
from app.services.billing.calendar import ImportCounts, ListedEvent
from app.services.billing.calendar_rules import NoLessonReason, normalize_name
from app.services.billing.payers import StudentPayer
from app.services.crm.inputs import require_storable_text

# A lesson longer than a day is a typo. Rule G (minutes divisible by 15) is request validation: the catalog has no
# class for it.
MAX_LESSON_MINUTES = 24 * 60
# A Postgres INTEGER; a larger value could not be bound. Rule G for a rate (divisible by 4) is the service's, so a
# lesson's own rate answers `rate_not_quarter_hour` like a student's rate does.
MAX_CENTS_PER_HOUR = 2**31 - 1
# One import reads at most this many days (billing spec, "Calendar import"): a quarter covers any
# catch-up, a longer range is most likely a typo, and it keeps one Graph read within its page and time limits.
MAX_IMPORT_DAYS = 92


# The instants a Python datetime can hold in UTC: one outside could neither be stored in UTC nor read back.
_FIRST_INSTANT = datetime.min.replace(tzinfo=UTC)
_LAST_INSTANT = datetime.max.replace(tzinfo=UTC)


def _in_utc(value: datetime) -> datetime:
    """Return the instant in UTC, as it is stored."""
    if not _FIRST_INSTANT <= value <= _LAST_INSTANT:
        raise ValueError("Value is out of range")
    return value.astimezone(UTC)


def _names_something(value: str) -> str:
    """A calendar name must keep something once normalised; the error repeats no part of it."""
    if not normalize_name(value):
        raise ValueError("Value must hold more than hyphens, underscores and spaces")
    return value


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
CalendarName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    AfterValidator(require_storable_text),
    AfterValidator(_names_something),
    Field(examples=["Erika Mustermann"]),
]
# A sevDesk id is a positive integer sent as a string; it is stored as given and checked against nothing. Only its
# canonical form passes - no leading zero - so one contact cannot be linked to two parties as "4711" and "04711".
SevdeskContactId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[1-9][0-9]*$"),
    Field(examples=["1234567"]),
]


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


class RateResponse(ApiModel):
    """A student's own rate from a day on; it holds until the day of the next one."""

    student_id: uuid.UUID
    """ID of the student's party."""
    valid_from: date
    """The day the rate takes effect."""
    cents_per_hour: int
    """The rate in cents per hour, divisible by 4. 45 and 90 minutes are priced linearly."""
    note: str | None
    """Free-text note."""
    created_at: datetime
    """When the rate was first set."""
    updated_at: datetime
    """When amount or note last changed."""

    @classmethod
    def from_model(cls, rate: Rate) -> Self:
        return cls(
            student_id=rate.student_party_id,
            valid_from=rate.valid_from,
            cents_per_hour=rate.cents_per_hour,
            note=rate.note,
            created_at=rate.created_at,
            updated_at=rate.updated_at,
        )


class RatePutRequest(ApiModel):
    """Body of `PUT /students/{party_id}/rates/{valid_from}`: the whole rate of that day.

    It replaces amount and note of a rate already set for the day; the same values again change nothing.
    """

    cents_per_hour: CentsPerHour
    """The rate in cents per hour: 0 or more, divisible by 4."""
    note: Note | None = None
    """Free-text note; leaving it out clears it."""


class StudentPayerResponse(ApiModel):
    """Who pays for a student's lessons: what a lesson that becomes billable now stores (PD1)."""

    student_id: uuid.UUID
    """ID of the student's party."""
    payer_id: uuid.UUID | None
    """The party that pays: the one party with a `pays_for` relation to the student - a person or a company - or the
    student itself when none has. `null` when several have."""
    ambiguous: bool
    """Whether several parties pay for the student: a lesson that becomes billable then stores no payer, and the
    CRM's `pays_for` relations are corrected first."""
    sevdesk_contact_linked: bool
    """Whether the payer has a sevDesk contact; without one there is no invoice draft. `false` while ambiguous."""
    manual_billing: bool
    """Whether the payer is billed by hand: their lessons never reach a draft. `false` while ambiguous."""

    @classmethod
    def from_model(cls, payer: StudentPayer) -> Self:
        return cls(
            student_id=payer.resolution.student_id,
            payer_id=payer.resolution.payer_id,
            ambiguous=payer.resolution.ambiguous,
            sevdesk_contact_linked=payer.sevdesk_contact_linked,
            manual_billing=payer.manual_billing,
        )


class LessonPayerResponse(ApiModel):
    """The payer stored on a lesson."""

    lesson_id: uuid.UUID
    """ID of the lesson."""
    student_id: uuid.UUID
    """ID of the student's party."""
    billable: bool
    """Whether the lesson is billable; only a billable lesson stores a payer."""
    payer_id: uuid.UUID | None
    """The payer stored on the lesson. `null` when the lesson is not billable, or when several parties pay for the
    student: such a lesson is not billed until it has a payer."""

    @classmethod
    def from_model(cls, lesson: Lesson) -> Self:
        return cls(
            lesson_id=lesson.id,
            student_id=lesson.student_party_id,
            billable=lesson.billable,
            payer_id=lesson.payer_party_id,
        )


class SevdeskContactPutRequest(ApiModel):
    """Body of `PUT /parties/{party_id}/sevdesk-contact`: the contact the party's invoices go to."""

    sevdesk_contact_id: SevdeskContactId
    """ID of the contact in sevDesk: a positive integer without leading zeros. It is stored as given; nothing is
    looked up in sevDesk."""


class SevdeskContactResponse(ApiModel):
    """The link between a party and its sevDesk contact."""

    party_id: uuid.UUID
    """ID of the party."""
    sevdesk_contact_id: str
    """ID of the contact in sevDesk."""
    created_at: datetime
    """When the link was made."""

    @classmethod
    def from_model(cls, contact: SevdeskContact) -> Self:
        return cls(party_id=contact.party_id, sevdesk_contact_id=contact.sevdesk_id, created_at=contact.created_at)


class ManualBillingPutRequest(ApiModel):
    """Body of `PUT /parties/{party_id}/manual-billing`: the whole mark. The same note again changes nothing."""

    note: Note | None = None
    """Free-text note, such as why the payer is billed by hand; leaving it out clears it."""


class ManualBillingResponse(ApiModel):
    """A payer billed by hand: their lessons show as "bill manually" and never reach a draft."""

    party_id: uuid.UUID
    """ID of the party."""
    note: str | None
    """Free-text note."""
    created_at: datetime
    """When the party was marked."""
    updated_at: datetime
    """When the note last changed."""

    @classmethod
    def from_model(cls, payer: ManualPayer) -> Self:
        return cls(party_id=payer.party_id, note=payer.note, created_at=payer.created_at, updated_at=payer.updated_at)


class CalendarAliasCreateRequest(ApiModel):
    """Body of `POST /calendar/aliases`: a name as written in the lesson calendar, and the student it means."""

    name: CalendarName
    """The name after ` mit ` in a calendar title. It is stored normalised - case, umlauts, hyphens and spaces do not
    count - and matches every spelling that normalises alike."""
    student_id: uuid.UUID
    """ID of a party holding the student role."""


class CalendarAliasResponse(ApiModel):
    """A calendar name mapped to a student: the import assigns an occurrence with this name to the student."""

    id: uuid.UUID
    """ID of the alias."""
    name_normalized: str
    """The name as the import compares it: case-folded, `ä ö ü ß` as `ae oe ue ss`, hyphens and underscores as
    spaces, single spaces."""
    student_id: uuid.UUID
    """ID of the student's party."""
    created_at: datetime
    """When the alias was stored."""

    @classmethod
    def from_model(cls, alias: CalendarAlias) -> Self:
        return cls(
            id=alias.id,
            name_normalized=alias.name_normalized,
            student_id=alias.student_party_id,
            created_at=alias.created_at,
        )


# The first and the last day of the calendar are refused: their Berlin midnight has no UTC datetime.
ImportDay = Annotated[date, Field(gt=date.min, lt=date.max, examples=["2026-11-02"])]


class CalendarImportRequest(ApiModel):
    """Body of `POST /calendar/import`: a range of days in `Europe/Berlin`, both included, at most 92 days."""

    first_day: ImportDay
    """The first day whose occurrences are imported."""
    last_day: ImportDay
    """The last day whose occurrences are imported; not before `first_day`."""

    @model_validator(mode="after")
    def _a_range(self) -> Self:
        if self.last_day < self.first_day:
            raise ValueError("last_day must not be before first_day")
        if (self.last_day - self.first_day).days >= MAX_IMPORT_DAYS:
            raise ValueError(f"A range holds at most {MAX_IMPORT_DAYS} days")
        return self


class CalendarImportResponse(ApiModel):
    """What an import did, in counts only."""

    occurrences: int
    """Occurrences read from the calendar and stored as seen."""
    lessons_created: int
    """Lessons the import proposed: `planned`, or `cancelled` for a late cancellation."""
    lessons_updated: int
    """`planned` lessons that followed their appointment: moved, resized or cancelled."""
    lessons_removed: int
    """`planned` lessons whose appointment vanished from the range."""
    changed: int
    """Occurrences of the range flagged `changed` (see `GET /calendar/events`): listed until dismissed - except one
    whose title names another student or subject, which is flagged again at every import until it is settled."""
    unassigned: int
    """Occurrences of the range that wait for an assignment."""
    deleted: int
    """Occurrences deleted: gone from the calendar without a lesson, or 90 days past their start without one."""

    @classmethod
    def from_model(cls, counts: ImportCounts) -> Self:
        return cls(
            occurrences=counts.occurrences,
            lessons_created=counts.lessons_created,
            lessons_updated=counts.lessons_updated,
            lessons_removed=counts.lessons_removed,
            changed=counts.changed,
            unassigned=counts.unassigned,
            deleted=counts.deleted,
        )


class CalendarEventResponse(ApiModel):
    """An occurrence of the lesson calendar that waits for an assignment, or differs from its lesson."""

    id: uuid.UUID
    """ID of the occurrence."""
    starts_at: datetime
    """When the appointment starts, in UTC, as last seen."""
    ends_at: datetime
    """When the appointment ends, in UTC, as last seen."""
    title_subject: str | None
    """The text before ` mit ` in the title, if it has one."""
    title_name: str | None
    """The name after ` mit `, as written, while the occurrence waits for an assignment; `null` otherwise."""
    cancelled: bool
    """Cancelled in the calendar or by the late-cancellation category."""
    reason: NoLessonReason | None
    """Why the rules give no lesson, as last seen - for an occurrence with a lesson too (an assigned one keeps the
    reason it waited for); `null` while the rules give a lesson, or when its student has a lesson at that start
    already."""
    changed: bool
    """The maintainer has to look: the title names another student or subject than the lesson's (flagged at every
    import until the lesson, the title or an alias settles it; a dismiss then clears it); the rules give no lesson;
    the appointment of a lesson the import does not change moved, was resized, cancelled or vanished; a lesson could
    not follow; or the occurrence could not get its lesson. Dismissing acknowledges all but another student or
    subject, until the occurrence changes again."""
    vanished: bool
    """Gone from the calendar while its lesson stays."""
    lesson_id: uuid.UUID | None
    """ID of the occurrence's lesson, if it has one."""
    last_seen_at: datetime
    """When an import last read the occurrence: before the last import of its day, it vanished from the calendar."""

    @classmethod
    def from_model(cls, listed: ListedEvent) -> Self:
        event = listed.event
        return cls(
            id=event.id,
            starts_at=event.starts_at,
            ends_at=event.ends_at,
            title_subject=event.title_subject,
            title_name=event.title_name,
            cancelled=event.cancelled,
            reason=NoLessonReason(event.reason) if event.reason is not None else None,
            changed=event.changed,
            vanished=event.vanished,
            lesson_id=listed.lesson_id,
            last_seen_at=event.last_seen_at,
        )


class CalendarEventAssignRequest(ApiModel):
    """Body of `POST /calendar/events/{event_id}/assign`: the lesson an occurrence without one stands for."""

    student_id: uuid.UUID
    """ID of a party holding the student role."""
    subject: SubjectLabel
    """The subject as a label (`Mathe`). Surrounding whitespace is stripped."""
    minutes: Minutes
    """The duration to bill, a multiple of 15: the appointment's, or a correction of it."""
    alias: CalendarName | None = None
    """A calendar name to store as the student's alias, so the import matches it from now on; usually `title_name`."""
