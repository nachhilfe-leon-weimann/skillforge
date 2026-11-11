"""The calendar rules (billing spec, "Calendar import", SF4-2): what an occurrence is, how a name matches, and that
the module stays pure. Every title and name is synthetic."""

import ast
import json
import sys
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.integrations.graph.models import CalendarEvent
from app.services.billing.calendar_rules import (
    Ambiguous,
    LessonProposal,
    NoLesson,
    NoLessonReason,
    Occurrence,
    ProposedStatus,
    Roster,
    StudentName,
    Title,
    Unmatched,
    is_phone_number,
    match_name,
    normalize_name,
    parse_title,
    propose,
)
from tests.api.test_crm_architecture import _imported_modules

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES = REPO_ROOT / "app/services/billing/calendar_rules.py"
GRAPH_FIXTURES = REPO_ROOT / "tests/fixtures/graph"

START = datetime(2026, 11, 2, 15, tzinfo=UTC)

ERIKA = uuid.UUID("00000000-0000-0000-0000-00000000000e")
MAX = uuid.UUID("00000000-0000-0000-0000-00000000000a")
PAULA = uuid.UUID("00000000-0000-0000-0000-00000000000b")
LENA = uuid.UUID("00000000-0000-0000-0000-00000000000c")
OTHER = uuid.UUID("00000000-0000-0000-0000-00000000000f")

ROSTER = Roster(
    students=(
        StudentName(ERIKA, "Erika", "Mustermann"),
        StudentName(MAX, "Max", "Mustermann"),
        StudentName(PAULA, "Paula", "Beispiel"),
        StudentName(LENA, "Lena", "Beispiel-Schmidt"),
    )
)
TWO_MAX = Roster(students=(StudentName(MAX, "Max", "Mustermann"), StudentName(OTHER, "Max", "Mustermann")))
LESSON = "Mathe mit Erika Mustermann"


def _occurrence(title: str = LESSON, *, minutes: float = 60, **fields) -> Occurrence:
    return Occurrence(title=title, starts_at=START, ends_at=START + timedelta(minutes=minutes), **fields)


def _lesson(status: ProposedStatus, *, student: uuid.UUID = ERIKA, subject: str = "Mathe", minutes: int = 60):
    return LessonProposal(student_id=student, subject=subject, minutes=minutes, status=status)


def _reason(proposal) -> NoLessonReason:
    assert isinstance(proposal, NoLesson), proposal
    return proposal.reason


# --- One test per row of the table, in its order ---


def test_row_1_an_all_day_entry_is_no_lesson():
    assert propose(_occurrence(all_day=True), ROSTER) == NoLesson(NoLessonReason.ALL_DAY)


@pytest.mark.parametrize(
    "fields",
    [
        {"categories": ("Telefon",)},
        {"categories": ("Teams", "telefon ")},
        {"location": "030 23125"},
        {"location": "+49 30 23125"},
        {"location": "030/23125-0"},
        {"location": " (030) 23125 "},
    ],
    ids=["category", "category-among-others-any-case", "number", "international", "slash-and-hyphen", "brackets"],
)
def test_row_2_a_phone_appointment_is_no_lesson(fields: dict):
    assert propose(_occurrence(**fields), ROSTER) == NoLesson(NoLessonReason.PHONE)


@pytest.mark.parametrize("title", ["Kennenlernen mit Jonas Probe", "kennenlernen MIT Erika Mustermann"])
def test_row_3_a_first_meeting_is_no_lesson_even_for_a_known_name(title: str):
    """``Kennenlernen`` is never read as a subject: not even when the name is a student's."""
    assert propose(_occurrence(title), ROSTER) == NoLesson(NoLessonReason.FIRST_MEETING)


@pytest.mark.parametrize(
    "title",
    [
        "Vorbereitung Klausur",
        "Kennenlernen",
        "Mathe mit",
        "Mathe mit  ",
        " mit Erika Mustermann",
        "Mathe-mit-Erika",
        "",
    ],
)
def test_row_4_a_title_without_the_pattern_is_no_lesson(title: str):
    assert propose(_occurrence(title), ROSTER) == NoLesson(NoLessonReason.NO_TITLE_PATTERN)


@pytest.mark.parametrize("minutes", [50, 61, 0, -60, 60.5])
def test_row_5_a_duration_off_the_quarter_hour_is_no_lesson(minutes: float):
    proposal = propose(_occurrence(minutes=minutes), ROSTER)

    assert proposal == NoLesson(NoLessonReason.DURATION, Title("Mathe", "Erika Mustermann"))


def test_row_6_a_name_nobody_has_is_no_lesson():
    proposal = propose(_occurrence("Mathe mit Jonas Probe"), ROSTER)

    assert proposal == NoLesson(NoLessonReason.UNMATCHED, Title("Mathe", "Jonas Probe"))


def test_row_7_a_name_several_students_have_is_no_lesson():
    proposal = propose(_occurrence("Mathe mit Max Mustermann"), TWO_MAX)

    assert proposal == NoLesson(NoLessonReason.AMBIGUOUS, Title("Mathe", "Max Mustermann"))


@pytest.mark.parametrize(
    "fields",
    [{"categories": ("Abgesagt",)}, {"categories": ("Teams", "Abgesagt")}, {"cancelled": True}],
    ids=["category", "among-other-categories", "is-cancelled"],
)
def test_row_8_a_late_cancellation_is_a_cancelled_lesson(fields: dict):
    assert propose(_occurrence(**fields), ROSTER) == _lesson(ProposedStatus.CANCELLED)


def test_row_9_anything_else_is_a_planned_lesson():
    assert propose(_occurrence(minutes=45), ROSTER) == _lesson(ProposedStatus.PLANNED, minutes=45)


@pytest.mark.parametrize(
    "fields",
    [
        {"online_meeting": True},
        {"location": "Microsoft Teams-Besprechung"},
        {"categories": ("Teams",)},
        {"online_meeting": True, "location": "Microsoft Teams-Besprechung", "categories": ("Teams",)},
    ],
    ids=["online-meeting", "teams-location", "teams-category", "all-three"],
)
def test_a_teams_lesson_is_a_lesson(fields: dict):
    """The legacy matcher skipped Teams meetings; billing must not (billing spec, "Matching")."""
    assert propose(_occurrence(**fields), ROSTER) == _lesson(ProposedStatus.PLANNED)


@pytest.mark.parametrize("location", ["", "Microsoft Teams-Besprechung", "Raum 12", "1.204", "1234", "Discord"])
def test_a_location_that_is_no_phone_number_keeps_the_lesson(location: str):
    assert not is_phone_number(location)
    assert propose(_occurrence(location=location), ROSTER) == _lesson(ProposedStatus.PLANNED)


def test_the_subject_is_the_text_before_the_first_mit_and_the_name_the_rest():
    assert parse_title("  Deutsch MIT Lena Beispiel-Schmidt mit Paula ") == Title(
        "Deutsch", "Lena Beispiel-Schmidt mit Paula"
    )
    assert propose(_occurrence("Physik mit Max Mustermann", minutes=120), ROSTER) == _lesson(
        ProposedStatus.PLANNED, student=MAX, subject="Physik", minutes=120
    )


# --- First match wins: an occurrence that fits two rows gets the earlier one ---


@pytest.mark.parametrize(
    ("occurrence", "reason"),
    [
        (_occurrence(all_day=True, categories=("Telefon",)), NoLessonReason.ALL_DAY),
        (_occurrence("Kennenlernen mit Jonas Probe", all_day=True), NoLessonReason.ALL_DAY),
        (_occurrence("Kennenlernen mit Jonas Probe", location="030 23125"), NoLessonReason.PHONE),
        (_occurrence("Vorbereitung Klausur", categories=("Telefon",)), NoLessonReason.PHONE),
        (_occurrence("Kennenlernen mit Jonas Probe", categories=("Abgesagt",)), NoLessonReason.FIRST_MEETING),
        (_occurrence("Kennenlernen mit Jonas Probe", minutes=50), NoLessonReason.FIRST_MEETING),
        (_occurrence("Vorbereitung Klausur", minutes=50), NoLessonReason.NO_TITLE_PATTERN),
        (_occurrence("Mathe mit Jonas Probe", minutes=50), NoLessonReason.DURATION),
        (_occurrence("Mathe mit Jonas Probe", cancelled=True), NoLessonReason.UNMATCHED),
        (_occurrence("Mathe mit Jonas Probe", categories=("Abgesagt",)), NoLessonReason.UNMATCHED),
    ],
    ids=[
        "all-day-before-phone",
        "all-day-before-first-meeting",
        "phone-before-first-meeting",
        "phone-before-no-title-pattern",
        "first-meeting-before-cancellation",
        "first-meeting-before-duration",
        "no-title-pattern-before-duration",
        "duration-before-unmatched",
        "unmatched-before-is-cancelled",
        "unmatched-before-late-cancellation",
    ],
)
def test_the_first_matching_row_wins(occurrence: Occurrence, reason: NoLessonReason):
    assert _reason(propose(occurrence, ROSTER)) == reason


def test_ambiguity_wins_over_a_late_cancellation():
    occurrence = _occurrence("Mathe mit Max Mustermann", categories=("Abgesagt",))

    assert _reason(propose(occurrence, TWO_MAX)) == NoLessonReason.AMBIGUOUS


def test_a_cancelled_phone_appointment_is_a_phone_appointment():
    assert _reason(propose(_occurrence(categories=("Abgesagt", "Telefon")), ROSTER)) == NoLessonReason.PHONE


def test_only_waiting_occurrences_carry_their_title():
    """Subject and name are handed on only where the occurrence waits for an assignment with a parsed title."""
    for occurrence in (
        _occurrence(all_day=True),
        _occurrence(categories=("Telefon",)),
        _occurrence("Kennenlernen mit Erika Mustermann"),
        _occurrence("Vorbereitung Klausur"),
    ):
        proposal = propose(occurrence, ROSTER)
        assert isinstance(proposal, NoLesson) and proposal.title is None


def test_no_proposal_and_no_input_shows_a_name_in_its_repr():
    occurrence = _occurrence("Mathe mit Jonas Probe", location="030 23125")
    proposal = propose(replace(occurrence, location=""), ROSTER)

    for value in (occurrence, proposal, ROSTER, parse_title(LESSON)):
        assert "Jonas" not in repr(value) and "Erika" not in repr(value) and "23125" not in repr(value)


# --- The recorded shapes: G3 and G4 ---


def _occurrences(fixture: str) -> list[Occurrence]:
    """Map a fixture's events onto ``Occurrence`` as the import will: through the Graph client's model."""
    body = json.loads((GRAPH_FIXTURES / fixture).read_text())["response"]["body"]
    events = [CalendarEvent.model_validate(raw) for raw in body["value"]]
    return [
        Occurrence(
            title=event.subject,
            starts_at=event.start,
            ends_at=event.end,
            all_day=event.is_all_day,
            cancelled=event.is_cancelled,
            categories=event.categories,
            location=event.location,
            online_meeting=event.is_online_meeting,
        )
        for event in events
    ]


def test_g3_late_cancellations_are_cancelled_lessons():
    proposals = [propose(occurrence, ROSTER) for occurrence in _occurrences("g3-late-cancellation.json")]

    assert proposals == [
        _lesson(ProposedStatus.CANCELLED, student=PAULA, subject="Informatik"),
        _lesson(ProposedStatus.CANCELLED, student=ERIKA, subject="Mathematik"),
    ]


def test_g4_keeps_the_teams_lesson_and_proposes_none_of_the_special_entries():
    proposals = [propose(occurrence, ROSTER) for occurrence in _occurrences("g4-special-entries.json")]

    assert proposals == [
        _lesson(ProposedStatus.PLANNED, student=MAX, subject="Physik"),
        NoLesson(NoLessonReason.PHONE),
        NoLesson(NoLessonReason.ALL_DAY),
        NoLesson(NoLessonReason.FIRST_MEETING),
        NoLesson(NoLessonReason.NO_TITLE_PATTERN),
    ]


# --- Normalisation and matching ---


@pytest.mark.parametrize(
    ("name", "normalized"),
    [
        ("Jürgen", "juergen"),
        ("Ｊｏｎａｓ", "jonas"),
        ("ERIKA", "erika"),
        ("Müller Öztürk Bär", "mueller oeztuerk baer"),
        ("MÜLLER", "mueller"),
        ("Weiß", "weiss"),
        ("Lena Beispiel-Schmidt", "lena beispiel schmidt"),
        ("Lena_Beispiel", "lena beispiel"),
        ("  Erika \t  Mustermann  ", "erika mustermann"),
        ("-_-", ""),
    ],
    ids=["nfkc", "nfkc-full-width", "casefold", "umlauts", "upper-case-umlaut", "sharp-s", "hyphen", "underscore",
         "spaces", "nothing-left"],
)  # fmt: skip
def test_normalisation_takes_each_step(name: str, normalized: str):
    assert normalize_name(name) == normalized


@pytest.mark.parametrize(
    "name",
    [
        "Erika Mustermann",
        "erika mustermann",
        "ERIKA MUSTERMANN",
        "Erika  Mustermann",
        " Erika Mustermann ",
        "Erika_Mustermann",
    ],
)
def test_an_exact_match_is_accepted_after_normalisation(name: str):
    assert match_name(name, ROSTER) == ERIKA


@pytest.mark.parametrize(
    "name",
    ["Jürgen Müller", "Juergen Mueller", "JÜRGEN MÜLLER", "Jürgen Müller", "JUERGEN  MUELLER"],
    ids=["composed", "transliterated", "upper-case", "decomposed", "upper-case-double-space"],
)
def test_umlauts_match_in_every_spelling(name: str):
    roster = Roster(students=(StudentName(OTHER, "Jürgen", "Müller"),))

    assert match_name(name, roster) == OTHER


@pytest.mark.parametrize("name", ["Lena Beispiel-Schmidt", "Lena Beispiel Schmidt", "lena beispiel_schmidt"])
def test_a_hyphenated_name_matches_with_or_without_its_hyphen(name: str):
    roster = Roster(students=(StudentName(LENA, "Lena", "Beispiel-Schmidt"), StudentName(PAULA, "Paula", "Beispiel")))

    assert match_name(name, roster) == LENA


@pytest.mark.parametrize("name", ["Max Mustermann", "Mustermann Max", "max peter"])
def test_the_one_student_whose_tokens_contain_the_names_tokens_matches(name: str):
    roster = Roster(students=(StudentName(MAX, "Max Peter", "Mustermann"), StudentName(PAULA, "Paula", "Beispiel")))

    assert match_name(name, roster) == MAX


@pytest.mark.parametrize("name", ["Erika", "Mustermann", "ERIKA", "Erika-", ""])
def test_a_one_token_name_is_never_a_containing_match(name: str):
    roster = Roster(students=(StudentName(ERIKA, "Erika", "Mustermann"),))

    assert match_name(name, roster) == Unmatched()


def test_a_one_token_student_name_still_matches_exactly():
    roster = Roster(students=(StudentName(ERIKA, "Erika", ""),))

    assert match_name("erika", roster) == ERIKA


def test_a_name_contained_in_two_students_is_ambiguous():
    roster = Roster(
        students=(StudentName(MAX, "Max Peter", "Mustermann"), StudentName(OTHER, "Max", "Mustermann-Schulz"))
    )

    assert match_name("Max Mustermann", roster) == Ambiguous()


def test_an_exact_match_wins_over_a_containing_match():
    roster = Roster(students=(StudentName(OTHER, "Max Peter", "Mustermann"), StudentName(MAX, "Max", "Mustermann")))

    assert match_name("Max Mustermann", roster) == MAX


def test_two_students_with_the_same_name_are_ambiguous():
    assert match_name("Max Mustermann", TWO_MAX) == Ambiguous()


def test_a_token_named_twice_must_be_there_twice():
    roster = Roster(students=(StudentName(ERIKA, "Erika Maria", "Mustermann"),))

    assert match_name("Erika Erika", roster) == Unmatched()


def test_a_name_with_no_student_is_unmatched():
    assert match_name("Jonas Probe", ROSTER) == Unmatched()


@pytest.mark.parametrize("roster", [ROSTER, TWO_MAX], ids=["over-an-exact-match", "over-an-ambiguity"])
def test_an_alias_wins(roster: Roster):
    """The alias decides even where a student has the name exactly, or several have it."""
    with_alias = replace(roster, aliases={"erika mustermann": OTHER, "max mustermann": OTHER})

    assert match_name("Erika Mustermann", with_alias) == OTHER
    assert match_name("MAX-Mustermann", with_alias) == OTHER


def test_an_alias_of_one_token_matches_and_is_proposed():
    roster = replace(ROSTER, aliases={"eri": PAULA})

    assert propose(_occurrence("Mathe mit Eri"), roster) == _lesson(ProposedStatus.PLANNED, student=PAULA)


# --- Pure: no session, no integration, no clock (money rule 7) ---


def _impurities(source: str, *, package: str = "app.services.billing") -> list[str]:
    """Return what makes a module impure: an import outside the standard library - SQLAlchemy, ``app.core.db``,
    an integration, another billing module - and any read of the clock."""
    found = sorted(
        module
        for module in _imported_modules(source, package=package)
        if module.split(".")[0] not in sys.stdlib_module_names or module.split(".")[0] == "time"
    )
    clock = {"now", "today", "utcnow", "time", "monotonic", "perf_counter", "time_ns", "localtime", "gmtime"}
    found += [
        f"{node.attr} at line {node.lineno}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute) and node.attr in clock
    ]
    return found


def test_the_calendar_rules_are_pure():
    assert _impurities(RULES.read_text()) == []


@pytest.mark.parametrize(
    "source",
    [
        "import sqlalchemy",
        "from sqlalchemy import select",
        "from sqlalchemy.ext.asyncio import AsyncSession",
        "from app.core.db import Database",
        "from app.core.db.models import Lesson",
        "from app.integrations.graph.models import CalendarEvent",
        "from app.integrations import graph",
        "import app.integrations.graph",
        "from .students import require_student",
        "from . import lessons",
        "from datetime import datetime\ndatetime.now()",
        "import datetime\ndatetime.datetime.now(tz=datetime.UTC)",
        "from datetime import datetime\nnow = datetime.now",
        "from datetime import date\ndate.today()",
        "from datetime import datetime\ndatetime.utcnow()",
        "import time\ntime.time()",
        "from time import monotonic",
        "import time",
    ],
)
def test_the_purity_check_catches_every_form(source: str):
    assert _impurities(source)


def test_the_purity_check_lets_the_standard_library_pass():
    allowed = (
        "import re\nimport unicodedata\nfrom collections import Counter\nfrom dataclasses import dataclass\n"
        "from datetime import datetime, time, timedelta\nfrom uuid import UUID"
    )

    assert _impurities(allowed) == []
