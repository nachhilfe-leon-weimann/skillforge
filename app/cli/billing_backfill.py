"""`just billing-backfill <file> [--apply]`: apply the payer mapping the maintainer confirmed (billing spec, SF3-2).

The maintainer's data step against the real database: an agent never runs it. Without `--apply` it checks the
file against itself and the database and prints what it would do; with `--apply` it writes the whole file in one
transaction through the services - `pays_for` through the CRM's relation service, the sevDesk contact link, the
manual-billing mark and the rate - or nothing. It only adds: a row that contradicts the database is a problem, never
an update, so a second run changes nothing.

The file: CSV, UTF-8 (a leading byte order mark is ignored), comma-separated, a header that names each column once,
in any order, and a row per student - or several: a fact given in more than one row is given the same way in each.

  student_party_id     required: the party id of a student
  payer_party_id       the party id of who pays for the student; the student's own id means "pays for themselves"
                       and writes no relation. It must be the student's one payer: the CRM holds it, or none.
  sevdesk_contact_id   the payer's sevDesk contact id: a positive integer, no leading zero; stored as given
  manual_billing       `true` or `false`: the payer is billed by hand; `false` for a marked payer is a problem
  rate_cents_per_hour  the student's own rate, rule G: divisible by 4
  rate_valid_from      its day, `YYYY-MM-DD`; a rate and its day come together. A day that has a rate keeps it.

An empty cell is "not given"; a contact or `manual_billing` needs a payer in its row. A party id is a hyphenated
UUID. The file is named by an absolute path to a regular file outside this repository and every other git work
tree, symlinks resolved, and refused before it is read and before anything connects. The output holds counts, line
numbers (the header is line 1) and reason codes - never a cell, an id or the database URL. One exception: before
anything connects, every run prints the smallest and the largest rate of the file's well-formed rows in euros per
hour, so euros typed as cents (`32` for 32 euros) show; rule G cannot catch them.
"""

import argparse
import asyncio
import csv
import io
import os
import re
import uuid
from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.billing.schemas import SevdeskContactId
from app.core.config import get_settings
from app.core.db import Database
from app.core.db.models import ManualPayer, PartyRelationType, Rate, SevdeskContact
from app.core.errors import DomainError
from app.integrations.sevdesk.invoices import euros
from app.services.billing import payers, rates
from app.services.billing.errors import RateNotQuarterHourError, UnknownStudentError
from app.services.billing.students import require_student
from app.services.crm import relations
from app.services.crm.errors import PartyNotFoundError
from app.services.crm.parties import load_party
from app.services.crm.payers import resolve_payer

REPO_ROOT = Path(__file__).resolve().parents[2]
COLUMNS = (
    "student_party_id",
    "payer_party_id",
    "sevdesk_contact_id",
    "manual_billing",
    "rate_cents_per_hour",
    "rate_valid_from",
)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE | re.ASCII)
_CONTACT_ID: TypeAdapter[str] = TypeAdapter(SevdeskContactId)  # the API's rule: canonical, no leading zero
_CENTS = re.compile(r"[0-9]{1,10}", re.ASCII)
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_MAX_CENTS = 2**31 - 1
_SERIALIZATION_FAILURE = "40001"
_FAILED = "billing-backfill failed, nothing written"


class MappingRefusedError(Exception):
    """The file as a whole is refused. The message says what is wrong, never a path or a cell."""


class Reason(StrEnum):
    MALFORMED_ROW = "malformed_row"
    MALFORMED_CELL = "malformed_cell"
    INCOMPLETE_RATE = "incomplete_rate"
    NEEDS_PAYER = "needs_payer"
    UNKNOWN_PARTY = "unknown_party"
    NOT_A_STUDENT = "not_a_student"
    SECOND_PAYER = "second_payer"
    CONTACT_CONFLICT = "contact_conflict"
    MANUAL_BILLING_CONFLICT = "manual_billing_conflict"
    RATE_NOT_QUARTER_HOUR = "rate_not_quarter_hour"
    RATE_CONFLICT = "rate_conflict"


class Kind(StrEnum):
    PAYS_FOR = "pays_for"
    SEVDESK_CONTACT = "sevdesk_contact"
    MANUAL_BILLING = "manual_billing"
    RATE = "rate"


@dataclass(frozen=True, order=True)
class Problem:
    line: int
    reason: Reason
    column: str | None = None

    def __str__(self) -> str:
        return f"line {self.line}: {self.reason}" + (f" ({self.column})" if self.column else "")


@dataclass(frozen=True)
class Row:
    line: int
    student_id: uuid.UUID
    payer_id: uuid.UUID | None
    contact_id: str | None
    manual_billing: bool | None
    rate: tuple[date, int] | None
    """The day and the cents per hour of the student's own rate."""


@dataclass(frozen=True)
class Mapping:
    rows: list[Row]
    """The well-formed rows; a malformed one is only a problem."""
    problems: list[Problem]
    count: int

    def lines(self) -> list[str]:
        """The row count and, if the rows give rates, the smallest and the largest in euros per hour."""
        cents = [row.rate[1] for row in self.rows if row.rate is not None]
        span = [f"rates: {euros(min(cents))} to {euros(max(cents))} euros per hour"] if cents else []
        return [f"rows: {self.count}", *span]


@dataclass
class Plan:
    """What the file asks for that the database does not hold yet, and its problems."""

    problems: list[Problem] = field(default_factory=list)
    pays_for: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)  # student -> payer
    contacts: dict[uuid.UUID, str] = field(default_factory=dict)  # payer -> contact id
    manual: dict[uuid.UUID, None] = field(default_factory=dict)  # payers to mark, in file order
    rates: dict[tuple[uuid.UUID, date], int] = field(default_factory=dict)
    unchanged: dict[Kind, set[Hashable]] = field(default_factory=lambda: defaultdict(set))

    def lines(self, *, applied: bool) -> list[str]:
        adds = dict(zip(Kind, map(len, (self.pays_for, self.contacts, self.manual, self.rates)), strict=True))
        verb = "added" if applied else "to add"
        return [f"{kind}: {adds[kind]} {verb}, {len(self.unchanged[kind])} unchanged" for kind in Kind]


# --- The file ---------------------------------------------------------------------------------------------------


def check_path(given: str) -> Path:
    """Return the real path of the file, or refuse it - before anything reads it."""
    if not os.path.isabs(given):
        raise MappingRefusedError("name the file by an absolute path")
    # Not normalised: a `..` after a symlink leads where the link points, for the OS and so for these checks.
    named = Path(given)
    if _in_git_work_tree(named):
        raise MappingRefusedError("the file lies inside a git work tree; keep it outside every repository")
    try:
        resolved = named.resolve(strict=True)
    except OSError, RuntimeError:
        raise MappingRefusedError("the file does not exist") from None
    if _in_git_work_tree(resolved):
        raise MappingRefusedError("the file lies inside a git work tree; keep it outside every repository")
    if not resolved.is_file():
        raise MappingRefusedError("the path names no regular file")
    return resolved


def _in_git_work_tree(path: Path) -> bool:
    return path.is_relative_to(REPO_ROOT) or any((folder / ".git").exists() for folder in (path, *path.parents))


def read_mapping(given: str) -> Mapping:
    """Check the path, then read and parse the file; a header or encoding fault refuses the whole file."""
    path = check_path(given)
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except OSError:  # its message names the path
        raise MappingRefusedError("the file cannot be read") from None
    except UnicodeDecodeError:
        raise MappingRefusedError("the file is not UTF-8") from None

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows: list[Row] = []
    problems: list[Problem] = []
    count = 0
    try:
        header = next(reader, [])
        if sorted(header) != sorted(COLUMNS):
            raise MappingRefusedError(f"the header must name each of these columns once: {', '.join(COLUMNS)}")
        for cells in reader:
            if not cells:
                continue
            count += 1
            parsed = _parse_row(reader.line_num, header, cells)
            if isinstance(parsed, Row):
                rows.append(parsed)
            else:
                problems += parsed
    except csv.Error:
        raise MappingRefusedError("the file is no well-formed CSV") from None
    return Mapping(rows, problems, count)


def _parse_row(line: int, header: list[str], cells: list[str]) -> Row | list[Problem]:
    if len(cells) != len(header):
        return [Problem(line, Reason.MALFORMED_ROW)]
    given = {column: cell for column, cell in zip(header, cells, strict=True) if cell}
    problems: list[Problem] = []

    def parse[T](column: str, parser: Callable[[str], T | None]) -> T | None:
        if column not in given:
            return None
        if (value := parser(given[column])) is None:
            problems.append(Problem(line, Reason.MALFORMED_CELL, column))
        return value

    student_id = parse("student_party_id", _party_id)
    payer_id = parse("payer_party_id", _party_id)
    contact_id = parse("sevdesk_contact_id", _contact_id)
    manual = parse("manual_billing", {"true": True, "false": False}.get)
    cents = parse("rate_cents_per_hour", _cents)
    day = parse("rate_valid_from", _day)
    if "student_party_id" not in given:
        problems.append(Problem(line, Reason.MALFORMED_CELL, "student_party_id"))
    if ("rate_cents_per_hour" in given) != ("rate_valid_from" in given):
        problems.append(Problem(line, Reason.INCOMPLETE_RATE))
    if "payer_party_id" not in given and ({"sevdesk_contact_id", "manual_billing"} & given.keys()):
        problems.append(Problem(line, Reason.NEEDS_PAYER))
    if problems or student_id is None:
        return problems
    rate = (day, cents) if day is not None and cents is not None else None
    return Row(line, student_id, payer_id, contact_id, manual, rate)


def _contact_id(cell: str) -> str | None:
    """The cell if it is a contact id in its one canonical form, as the API takes it - taken verbatim, not stripped."""
    try:
        return cell if _CONTACT_ID.validate_python(cell) == cell else None
    except ValidationError:
        return None


def _party_id(cell: str) -> uuid.UUID | None:
    return uuid.UUID(cell) if _UUID.fullmatch(cell) else None


def _cents(cell: str) -> int | None:
    return int(cell) if _CENTS.fullmatch(cell) and int(cell) <= _MAX_CENTS else None


def _day(cell: str) -> date | None:
    try:
        return date.fromisoformat(cell) if _DAY.fullmatch(cell) else None
    except ValueError:
        return None


# --- Checking and writing ---------------------------------------------------------------------------------------

# A fact given in several rows is given the same way: whose fact, the fact, and the reason when rows disagree.
_AGREEMENTS: tuple[tuple[Callable[[Row], Hashable], Callable[[Row], Hashable], Reason], ...] = (
    (lambda row: row.student_id, lambda row: row.payer_id, Reason.SECOND_PAYER),
    (lambda row: row.payer_id, lambda row: row.contact_id, Reason.CONTACT_CONFLICT),
    (lambda row: row.contact_id, lambda row: row.payer_id, Reason.CONTACT_CONFLICT),
    (lambda row: row.payer_id, lambda row: row.manual_billing, Reason.MANUAL_BILLING_CONFLICT),
    (lambda row: row.rate and (row.student_id, row.rate[0]), lambda row: row.rate, Reason.RATE_CONFLICT),
)


@dataclass(frozen=True)
class _Stored:
    """What the database holds of the file's parties, read once."""

    contact_of: dict[uuid.UUID, str]
    party_of: dict[str, uuid.UUID]
    marked: set[uuid.UUID]
    rates: dict[tuple[uuid.UUID, date], int]

    @classmethod
    async def read(cls, session: AsyncSession, rows: list[Row]) -> _Stored:
        payer_ids = {row.payer_id for row in rows if row.payer_id is not None}
        # Every link, read by its columns only (the architecture test's rule); there is one per payer at most.
        links = await session.execute(select(SevdeskContact.party_id, SevdeskContact.sevdesk_id))
        contact_of = {party_id: contact_id for party_id, contact_id in links}
        marked = await session.scalars(select(ManualPayer.party_id).where(ManualPayer.party_id.in_(payer_ids)))
        stored_rates = await session.execute(
            select(Rate.student_party_id, Rate.valid_from, Rate.cents_per_hour).where(
                Rate.student_party_id.in_({row.student_id for row in rows})
            )
        )
        return cls(
            contact_of=contact_of,
            party_of={contact_id: party_id for party_id, contact_id in contact_of.items()},
            marked=set(marked),
            rates={(student_id, day): cents for student_id, day, cents in stored_rates},
        )


async def plan_mapping(session: AsyncSession, mapping: Mapping) -> Plan:
    """Check every row against the file and the database and collect what is to be added. It writes nothing."""
    plan = Plan(problems=list(mapping.problems))
    for whose, fact, reason in _AGREEMENTS:
        plan.problems += _disagreeing(mapping.rows, whose, fact, reason)

    stored = await _Stored.read(session, mapping.rows)
    students: dict[uuid.UUID, Reason | uuid.UUID | None] = {}
    known: dict[uuid.UUID, bool] = {}
    for row in mapping.rows:
        if row.student_id not in students:
            students[row.student_id] = await _crm_payer(session, row.student_id)
        if row.payer_id is not None and row.payer_id not in known:
            known[row.payer_id] = await _exists(session, row.payer_id)
        if problems := _check(row, students[row.student_id], known, stored):
            plan.problems += problems
        else:
            _collect(plan, row, students[row.student_id], stored)
    plan.problems = sorted(set(plan.problems), key=lambda problem: (problem.line, problem.reason, problem.column or ""))
    return plan


async def _crm_payer(session: AsyncSession, student_id: uuid.UUID) -> Reason | uuid.UUID | None:
    """The student's payer in the CRM (``None`` while ambiguous), or why the party is no student."""
    try:
        await require_student(session, student_id)
    except PartyNotFoundError:
        return Reason.UNKNOWN_PARTY
    except UnknownStudentError:
        return Reason.NOT_A_STUDENT
    return (await resolve_payer(session, student_id)).payer_id


async def _exists(session: AsyncSession, party_id: uuid.UUID) -> bool:
    try:
        await load_party(session, party_id)
    except PartyNotFoundError:
        return False
    return True


def _disagreeing(
    rows: Iterable[Row], whose: Callable[[Row], Hashable], fact: Callable[[Row], Hashable], reason: Reason
) -> list[Problem]:
    given = [(row, key, value) for row in rows if (key := whose(row)) is not None and (value := fact(row)) is not None]
    facts: defaultdict[Hashable, set[Hashable]] = defaultdict(set)
    for _, key, value in given:
        facts[key].add(value)
    return [Problem(row.line, reason) for row, key, _ in given if len(facts[key]) > 1]


def _check(
    row: Row, crm_payer: Reason | uuid.UUID | None, known: dict[uuid.UUID, bool], stored: _Stored
) -> list[Problem]:
    """The row's problems with the database: each a reason the whole file is refused."""
    problems: list[Problem] = []
    if isinstance(crm_payer, Reason):
        problems.append(Problem(row.line, crm_payer, "student_party_id"))
    if (payer := row.payer_id) is not None:
        if payer != row.student_id and not known[payer]:
            problems.append(Problem(row.line, Reason.UNKNOWN_PARTY, "payer_party_id"))
        # One payer: the one the CRM holds, or none yet - the student itself. `None` is several.
        if not isinstance(crm_payer, Reason) and crm_payer not in (payer, row.student_id):
            problems.append(Problem(row.line, Reason.SECOND_PAYER))
        contact = row.contact_id
        if contact is not None and contact != stored.contact_of.get(payer, contact):
            problems.append(Problem(row.line, Reason.CONTACT_CONFLICT))
        if contact is not None and payer != stored.party_of.get(contact, payer):
            problems.append(Problem(row.line, Reason.CONTACT_CONFLICT))
        if row.manual_billing is False and payer in stored.marked:
            problems.append(Problem(row.line, Reason.MANUAL_BILLING_CONFLICT))
    if row.rate is not None:
        day, cents = row.rate
        try:
            rates.require_quarter_hour_rate(cents)
        except RateNotQuarterHourError:
            problems.append(Problem(row.line, Reason.RATE_NOT_QUARTER_HOUR))
        if stored.rates.get((row.student_id, day), cents) != cents:
            problems.append(Problem(row.line, Reason.RATE_CONFLICT))
    return problems


def _collect(plan: Plan, row: Row, crm_payer: Reason | uuid.UUID | None, stored: _Stored) -> None:
    """Note what a checked row adds and what the database already holds as it says."""
    if (payer := row.payer_id) is not None:
        if payer == crm_payer:  # the CRM holds the relation, or the student pays for themselves
            plan.unchanged[Kind.PAYS_FOR].add(row.student_id)
        else:
            plan.pays_for[row.student_id] = payer
        if row.contact_id is not None and stored.contact_of.get(payer) == row.contact_id:
            plan.unchanged[Kind.SEVDESK_CONTACT].add(payer)
        elif row.contact_id is not None:
            plan.contacts[payer] = row.contact_id
        if row.manual_billing is not None and row.manual_billing == (payer in stored.marked):
            plan.unchanged[Kind.MANUAL_BILLING].add(payer)
        elif row.manual_billing:
            plan.manual[payer] = None
    if row.rate is not None:
        day, cents = row.rate
        if stored.rates.get((row.student_id, day)) == cents:
            plan.unchanged[Kind.RATE].add((row.student_id, day))
        else:
            plan.rates[(row.student_id, day)] = cents


async def write_plan(session: AsyncSession, plan: Plan) -> None:
    """Add what the plan holds, through the services."""
    for student_id, payer_id in plan.pays_for.items():
        await relations.put_relation(session, payer_id, PartyRelationType.PAYS_FOR, student_id)
    for payer_id, contact_id in plan.contacts.items():
        await payers.link_sevdesk_contact(session, payer_id, contact_id)
    for payer_id in plan.manual:
        await payers.put_manual_payer(session, payer_id)
    for (student_id, day), cents in plan.rates.items():
        await rates.put_rate(session, student_id, day, cents_per_hour=cents)


# --- The command ------------------------------------------------------------------------------------------------


async def billing_backfill(file: str, *, apply: bool) -> None:
    """Refuse a bad path or file before anything connects, check every row, then write all of it or nothing.

    A dry run reads one snapshot (``REPEATABLE READ``) in a transaction that is rolled back. ``--apply`` checks and
    writes in one ``SERIALIZABLE`` transaction. Another write committed between its check and its write that collides
    with what it writes - a party row both update, a key its insert meets, a payer deleted under its foreign key -
    fails the whole run ("run it again"); one that collides with nothing ends as if it had come right after the run,
    and nothing is overwritten. ``SERIALIZABLE`` adds a second backfill at the same time: one of the two fails. A
    failure prints its code or its class, never its message: those name ids, cells and the URL.
    """
    try:
        mapping = read_mapping(file)
    except MappingRefusedError as exc:
        raise SystemExit(f"billing-backfill refused: {exc}") from None
    for line in mapping.lines():
        print(line)

    try:
        db = Database.from_url(str(get_settings().db.url))
    except ValueError, SQLAlchemyError:  # a settings or URL error repeats the URL
        raise SystemExit("billing-backfill refused: the database settings cannot be read") from None
    try:
        async with db.session(write=apply, isolation_level="SERIALIZABLE" if apply else "REPEATABLE READ") as session:
            plan = await plan_mapping(session, mapping)
            if apply and not plan.problems:
                await write_plan(session, plan)
    except DomainError as exc:
        raise SystemExit(f"{_FAILED}: {exc.code}") from None
    except SQLAlchemyError as exc:
        if getattr(getattr(exc, "orig", None), "sqlstate", None) == _SERIALIZATION_FAILURE:
            raise SystemExit(f"{_FAILED}: the data changed during the run; run it again") from None
        raise SystemExit(f"{_FAILED}: {type(exc).__name__}") from None
    except OSError as exc:  # connecting: the message names the host and the port
        raise SystemExit(f"{_FAILED}: {type(exc).__name__}") from None
    finally:
        await db.dispose()

    if plan.problems:
        for problem in plan.problems:
            print(problem)
        raise SystemExit(f"billing-backfill refused: {len(plan.problems)} problems; nothing written")
    for line in plan.lines(applied=apply):
        print(line)
    print("written in one transaction" if apply else "dry run: nothing written; pass --apply to write")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="just billing-backfill", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("file", help="absolute path of the mapping CSV, outside every git work tree")
    parser.add_argument("--apply", action="store_true", help="write; without it nothing is written")
    return parser


def main() -> None:
    arguments = build_parser().parse_args()
    asyncio.run(billing_backfill(arguments.file, apply=arguments.apply))


if __name__ == "__main__":
    main()
