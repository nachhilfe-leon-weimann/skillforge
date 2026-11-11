"""What an occurrence of the lesson calendar is: a lesson for one student, or no lesson and why (billing spec,
"Calendar import").

Pure: plain data in, plain data out - no session, no network, no clock (money rule 7, architecture test). The import
maps Graph's events onto ``Occurrence`` and the students and aliases it read onto ``Roster``; nothing here knows
either. The match tokens are German matching data, not family-visible text, and live only here. A title holds a
name: nothing here logs, and no exception carries a title.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

# The match tokens (billing spec, "Match tokens are not family-visible text").
TITLE_SEPARATOR = "mit"
"""``<Fach> mit <Vorname Nachname>``: the subject before the first ``mit``, the name after it."""
FIRST_MEETING = "Kennenlern"
"""A subject that contains it, normalised, is the free first meeting - ``Kennenlernen``, ``Erstes Kennenlernen``,
``Kennenlerngespräch`` - and never a subject of a lesson."""
PHONE_CATEGORY = "Telefon"
LATE_CANCELLATION_CATEGORY = "Abgesagt"
"""The category of a late cancellation and a no-show alike (Q9)."""

# Lessons are planned in quarter hours (decision G).
LESSON_STEP = timedelta(minutes=15)
# The longest lesson: a longer one is a typo. The same bound as `MAX_LESSON_MINUTES` of the lesson routes, which this
# pure module cannot import (a test ties the two together).
MAX_LESSON_DURATION = timedelta(hours=24)

_TITLE_SEPARATOR = re.compile(rf" {TITLE_SEPARATOR} ", re.IGNORECASE)
# A location that is a phone number: an optional leading `+`, then only digits, spaces and `/ ( ) -`, with 5 to 15
# digits (15 is E.164's longest number). A room, an address or a Teams location has letters; a shorter number alone
# is more likely a room, a longer one an id.
_PHONE_NUMBER = re.compile(r"\+?[0-9 /()-]+")
_PHONE_DIGITS = range(5, 16)
# `ß` (and `ẞ`) needs no entry: `casefold` has made it `ss` already.
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue"})
_WORD_SEPARATORS = str.maketrans("-_", "  ")


class NoLessonReason(StrEnum):
    """Why an occurrence is no lesson: ``calendar_event.reason``."""

    ALL_DAY = "all_day"
    PHONE = "phone"
    FIRST_MEETING = "first_meeting"
    NO_TITLE_PATTERN = "no_title_pattern"
    DURATION = "duration"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"


class ProposedStatus(StrEnum):
    """The status a proposed lesson starts with; the values are ``billing.lesson_status``'s."""

    PLANNED = "planned"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Occurrence:
    """One occurrence as the calendar shows it."""

    title: str = field(repr=False)
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False
    cancelled: bool = False
    """Cancelled in the calendar (``isCancelled``)."""
    categories: tuple[str, ...] = ()
    location: str = field(default="", repr=False)
    """The location's display name."""
    online_meeting: bool = False
    """A Teams meeting (``isOnlineMeeting``). Carried and never read: a Teams lesson is a lesson (decision N)."""

    def __post_init__(self) -> None:
        # The duration is measured between instants: a wall time without its offset is none.
        if self.starts_at.utcoffset() is None or self.ends_at.utcoffset() is None:
            raise ValueError("An occurrence starts and ends at aware datetimes")


@dataclass(frozen=True)
class StudentName:
    """A student the import can match: the party with the first and last name of its person."""

    student_id: UUID
    firstname: str = field(repr=False)
    lastname: str = field(repr=False)


@dataclass(frozen=True)
class Roster:
    """Who a calendar name can match: the students, and the aliases by normalised name (``billing.calendar_alias``)."""

    students: tuple[StudentName, ...] = ()
    aliases: Mapping[str, UUID] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Title:
    """A title split at the first `` mit ``: both parts stripped and never empty."""

    subject: str = field(repr=False)
    name: str = field(repr=False)


@dataclass(frozen=True)
class LessonProposal:
    """The occurrence is a lesson of this student."""

    student_id: UUID
    subject: str
    minutes: int
    status: ProposedStatus


@dataclass(frozen=True)
class NoLesson:
    """The occurrence is no lesson, for ``reason``.

    ``title`` is set for the reasons that leave the occurrence waiting for an assignment with a title that has a
    subject and a name - ``duration``, ``unmatched`` and ``ambiguous`` - and ``None`` otherwise.
    """

    reason: NoLessonReason
    title: Title | None = field(default=None, repr=False)


type Proposal = LessonProposal | NoLesson


@dataclass(frozen=True)
class Unmatched:
    """No student has the name."""


@dataclass(frozen=True)
class Ambiguous:
    """Several students have the name: never a guess."""


type NameMatch = UUID | Unmatched | Ambiguous


def propose(occurrence: Occurrence, roster: Roster) -> Proposal:
    """Decide what the occurrence is. The rules run in the order of the spec's table; the first that applies wins."""
    if occurrence.all_day:
        return NoLesson(NoLessonReason.ALL_DAY)
    if _has_category(occurrence, PHONE_CATEGORY) or is_phone_number(occurrence.location):
        return NoLesson(NoLessonReason.PHONE)

    title = parse_title(occurrence.title)
    if title is not None and normalize_name(FIRST_MEETING) in normalize_name(title.subject):
        return NoLesson(NoLessonReason.FIRST_MEETING)
    if title is None:
        return NoLesson(NoLessonReason.NO_TITLE_PATTERN)

    # Between instants: aware datetimes of one zone would subtract as wall times, wrong across a clock change.
    duration = occurrence.ends_at.astimezone(UTC) - occurrence.starts_at.astimezone(UTC)
    if (
        not timedelta() < duration <= MAX_LESSON_DURATION
        or duration % LESSON_STEP
        or not (_on_the_minute(occurrence.starts_at) and _on_the_minute(occurrence.ends_at))
    ):
        return NoLesson(NoLessonReason.DURATION, title)

    student_id = match_name(title.name, roster)
    if isinstance(student_id, Unmatched):
        return NoLesson(NoLessonReason.UNMATCHED, title)
    if isinstance(student_id, Ambiguous):
        return NoLesson(NoLessonReason.AMBIGUOUS, title)

    return LessonProposal(
        student_id=student_id,
        subject=title.subject,
        minutes=duration // timedelta(minutes=1),
        status=ProposedStatus.CANCELLED if is_cancelled(occurrence) else ProposedStatus.PLANNED,
    )


def is_cancelled(occurrence: Occurrence) -> bool:
    """Whether the occurrence is cancelled: in the calendar (``isCancelled``) or by the late-cancellation category."""
    return occurrence.cancelled or _has_category(occurrence, LATE_CANCELLATION_CATEGORY)


def parse_title(title: str) -> Title | None:
    """Split ``<subject> mit <name>`` at the first `` mit `` (case-insensitive); ``None`` without one, or when either
    side is empty."""
    parts = _TITLE_SEPARATOR.split(title, maxsplit=1)
    if len(parts) != 2:
        return None

    subject, name = (part.strip() for part in parts)
    return Title(subject, name) if subject and name else None


def match_name(name: str, roster: Roster) -> NameMatch:
    """Find the one student a calendar name means.

    An alias wins - if it names one of the students; one that names a party outside the roster (a former student)
    is unmatched, never a lesson. Otherwise the normalised name is compared with each student's
    ``firstname lastname``: first an exact match; then, for a name of at least two tokens, the one student whose
    name tokens contain all of its tokens (a token named twice must be there twice). Several students at either step
    are ambiguous. A name that normalises to nothing matches nobody.
    """
    normalized = normalize_name(name)
    if not normalized:
        return Unmatched()
    if normalized in roster.aliases:
        aliased = roster.aliases[normalized]
        return aliased if any(student.student_id == aliased for student in roster.students) else Unmatched()

    names = {
        student.student_id: normalize_name(f"{student.firstname} {student.lastname}") for student in roster.students
    }
    exact = {student_id for student_id, student_name in names.items() if student_name == normalized}
    if exact:
        return _one(exact)

    tokens = Counter(normalized.split())
    if tokens.total() < 2:
        return Unmatched()
    return _one({student_id for student_id, student_name in names.items() if tokens <= Counter(student_name.split())})


def normalize_name(name: str) -> str:
    """NFKC, casefold (which turns ``ß`` into ``ss``), ``ä ö ü`` to ``ae oe ue``, hyphens and underscores to spaces,
    single spaces."""
    text = unicodedata.normalize("NFKC", name)
    text = text.casefold()
    text = text.translate(_UMLAUTS)
    text = text.translate(_WORD_SEPARATORS)
    return " ".join(text.split())


def is_phone_number(location: str) -> bool:
    """Whether a location is a phone number: see ``_PHONE_NUMBER``."""
    text = location.strip()
    return _PHONE_NUMBER.fullmatch(text) is not None and sum(char.isdigit() for char in text) in _PHONE_DIGITS


def _on_the_minute(moment: datetime) -> bool:
    """Whether the instant is a whole minute. Graph's times carry seconds and fractions: a lesson at ``16:00:30`` would
    stand beside one set at ``16:00`` (``uq_lesson_student_start``), and nothing rounds a time."""
    instant = moment.astimezone(UTC)
    return not (instant.second or instant.microsecond)


def _has_category(occurrence: Occurrence, category: str) -> bool:
    wanted = category.casefold()
    return any(given.strip().casefold() == wanted for given in occurrence.categories)


def _one(student_ids: set[UUID]) -> NameMatch:
    match len(student_ids):
        case 0:
            return Unmatched()
        case 1:
            return next(iter(student_ids))
    return Ambiguous()
