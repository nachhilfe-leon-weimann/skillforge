"""`just seed-demo`: synthetic families, students, payers, rates and lessons for local screens (billing spec, SF2-3).

It writes only to a local database: the configured URL must use the app's driver (`postgresql+asyncpg`), carry no
query parameters, and name `localhost`, a loopback address or a single-label host name such as a compose service.
Anything else is refused before a connection opens. It writes through the CRM and billing services. Its lessons fall
in the current Berlin month and the one before: run again in the same month it changes no row, in a later month it
adds that month's lessons. It prints the class of the host and counts - never the URL, a password or another
setting.
"""

import argparse
import asyncio
import ipaddress
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import Database
from app.core.db.models import ContactInfo, ContactInfoType, LessonStatus, PartyRelationType, PreferredMeetingTool
from app.core.errors import DomainError
from app.services.billing import lessons, rates
from app.services.billing.errors import LessonAlreadyExistsError
from app.services.crm import persons, relations
from app.services.crm.inputs import NewContactInfo, StudentRoleData, normalize_contact_value

# The driver the app connects with; it takes the host from the URL alone.
APP_DRIVER = "postgresql+asyncpg"
# One DNS label: what a compose service or a container is called. A dot makes a name that DNS resolves elsewhere.
_SINGLE_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?", re.IGNORECASE | re.ASCII)
# A label the C resolver reads as an IPv4 address: decimal or octal digits (`2130706433`), or hex (`0x7f000001`).
_NUMERIC_ADDRESS = re.compile(r"\d+|0x[0-9a-f]*", re.IGNORECASE | re.ASCII)


class HostClass(StrEnum):
    """The kinds of host `seed-demo` accepts as local."""

    LOCALHOST = "localhost"
    LOOPBACK = "loopback address"
    SINGLE_LABEL = "single-label host name"


class NotLocalError(Exception):
    """The database URL names no local host. The message names what kind of host it is, never the host."""


def local_host_class(url: str) -> HostClass:
    """Return the class of the URL's host if it is local; raise ``NotLocalError`` otherwise, failing closed.

    A loopback address (IPv4 or IPv6), ``localhost`` and a single-label name pass. Refused: a name with a dot, an
    address that is not loopback, a label the resolver would read as an address (``2130706433``), no host, several
    hosts, a URL that cannot be parsed, any driver but the app's (another one may read the host from its environment,
    as libpq does with ``PGHOSTADDR`` and ``PGSERVICE``) - and any query parameter, since one (``host``) can name
    another host.
    """
    try:
        parsed = make_url(url)
    except ArgumentError, ValueError:
        raise NotLocalError("the database URL cannot be parsed") from None
    if parsed.drivername != APP_DRIVER:
        raise NotLocalError(f"the database URL does not use the driver {APP_DRIVER}")
    if parsed.query:
        raise NotLocalError("the database URL carries query parameters")
    host = parsed.host
    if not host:
        raise NotLocalError("the database URL names no host")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if address.is_loopback:
            return HostClass.LOOPBACK
        raise NotLocalError("the host is an address that is not loopback")

    if host.lower() == "localhost":
        return HostClass.LOCALHOST
    if "." in host:
        raise NotLocalError("the host is a fully qualified name")
    if not _SINGLE_LABEL.fullmatch(host) or _NUMERIC_ADDRESS.fullmatch(host):
        raise NotLocalError("the host is no plain host name")
    return HostClass.SINGLE_LABEL


# --- The demo data ---------------------------------------------------------------------------------------------
# Family A's parent pays for students B and C, siblings with different rates; student D pays for themselves and has
# the default rate. Every address ends in example.org, so a person is found again on the next run.


@dataclass(frozen=True)
class DemoPerson:
    firstname: str
    lastname: str
    email: str
    student: bool


@dataclass(frozen=True)
class DemoRate:
    student: str
    valid_from: date
    cents_per_hour: int
    note: str | None = None


@dataclass(frozen=True)
class DemoLesson:
    student: str
    previous_month: bool
    """In the month before the current one - ended and confirmed - or in the current one, still planned."""
    day: int
    hour: int
    minutes: int
    subject: str
    status: LessonStatus = LessonStatus.PLANNED
    billable: bool | None = None
    """``None``: the status's default - the tick makes a lesson billable."""
    rate_cents_per_hour: int | None = None


PERSONS = {
    "A": DemoPerson("Parent", "A", "family-a@demo.example.org", student=False),
    "B": DemoPerson("Student", "B", "student-b@demo.example.org", student=True),
    "C": DemoPerson("Student", "C", "student-c@demo.example.org", student=True),
    "D": DemoPerson("Student", "D", "student-d@demo.example.org", student=True),
}

RELATIONS = (
    ("A", PartyRelationType.PARENT_OF, "B"),
    ("A", PartyRelationType.PARENT_OF, "C"),
    ("A", PartyRelationType.PAYS_FOR, "B"),
    ("A", PartyRelationType.PAYS_FOR, "C"),
)

RATES = (
    DemoRate("B", date(2026, 1, 1), 3200),
    DemoRate("C", date(2026, 1, 1), 2800),
    DemoRate("C", date(2026, 9, 1), 3000, note="Demo: a rate change"),
)

LESSONS = (
    DemoLesson("B", True, 3, 16, 60, "Mathe", LessonStatus.HELD),
    DemoLesson("B", True, 10, 16, 45, "Mathe", LessonStatus.HELD, billable=False),
    DemoLesson("B", True, 17, 16, 60, "Mathe", LessonStatus.CANCELLED, billable=True),
    DemoLesson("C", True, 4, 15, 90, "Englisch", LessonStatus.HELD),
    DemoLesson("C", True, 11, 15, 60, "Englisch", LessonStatus.CANCELLED),
    DemoLesson("C", True, 18, 15, 60, "Englisch", LessonStatus.NOT_HELD),
    DemoLesson("D", True, 5, 18, 60, "Physik", LessonStatus.HELD, rate_cents_per_hour=2000),
    DemoLesson("D", True, 12, 18, 30, "Physik", LessonStatus.HELD),
    DemoLesson("B", False, 24, 16, 60, "Mathe"),
    DemoLesson("C", False, 25, 15, 60, "Englisch"),
    DemoLesson("D", False, 26, 18, 60, "Physik"),
)


@dataclass(frozen=True)
class SeedReport:
    persons_created: int
    lessons_created: int
    relations: int
    rates: int

    def lines(self) -> list[str]:
        return [
            f"persons: {self.persons_created} created, {len(PERSONS) - self.persons_created} already there",
            f"relations: {self.relations} in place",
            f"rates: {self.rates} in place",
            f"lessons: {self.lessons_created} created, {len(LESSONS) - self.lessons_created} already there",
        ]


async def seed(session: AsyncSession, *, today: date) -> SeedReport:
    """Write the demo data through the services. The lessons fall in the month before ``today``'s and in its own,
    so a rerun in the same month changes nothing."""
    party_ids: dict[str, uuid.UUID] = {}
    persons_created = 0
    for key, person in PERSONS.items():
        party_ids[key], created = await _ensure_person(session, person)
        persons_created += created

    for from_key, relation_type, to_key in RELATIONS:
        await relations.put_relation(session, party_ids[from_key], relation_type, party_ids[to_key])
    for rate in RATES:
        await rates.put_rate(
            session, party_ids[rate.student], rate.valid_from, cents_per_hour=rate.cents_per_hour, note=rate.note
        )

    current_month = today.replace(day=1)
    previous_month = (current_month - timedelta(days=1)).replace(day=1)
    lessons_created = 0
    for lesson in LESSONS:
        month = previous_month if lesson.previous_month else current_month
        lessons_created += await _ensure_lesson(session, party_ids[lesson.student], lesson, month=month)

    return SeedReport(persons_created, lessons_created, relations=len(RELATIONS), rates=len(RATES))


async def _ensure_person(session: AsyncSession, person: DemoPerson) -> tuple[uuid.UUID, bool]:
    """The party carrying the person's address, created with it when there is none."""
    address = normalize_contact_value(ContactInfoType.EMAIL, person.email)
    party_id = await session.scalar(
        select(ContactInfo.party_id)
        .where(ContactInfo.type == ContactInfoType.EMAIL, ContactInfo.value == address)
        .order_by(ContactInfo.party_id)
        .limit(1)
    )
    if party_id is not None:
        return party_id, False

    party = await persons.create_person(
        session,
        firstname=person.firstname,
        lastname=person.lastname,
        contact_infos=[NewContactInfo(ContactInfoType.EMAIL, address)],
        student=StudentRoleData(preferred_meeting_tool=PreferredMeetingTool.DISCORD) if person.student else None,
    )
    return party.id, True


async def _ensure_lesson(session: AsyncSession, student_id: uuid.UUID, lesson: DemoLesson, *, month: date) -> bool:
    """Create the lesson and bring it to its status; an existing one at that start is left as it is."""
    starts_at = datetime.combine(month.replace(day=lesson.day), time(lesson.hour), tzinfo=lessons.BILLING_TIME_ZONE)
    try:
        created = await lessons.create_lesson(
            session,
            student_id=student_id,
            subject=lesson.subject,
            starts_at=starts_at,
            minutes=lesson.minutes,
            rate_cents_per_hour=lesson.rate_cents_per_hour,
        )
    except LessonAlreadyExistsError:
        return False

    await lessons.update_lesson(session, created.id, status=lesson.status, billable=lesson.billable)
    return True


# --- The command ------------------------------------------------------------------------------------------------


async def seed_demo(*, today: date | None = None) -> None:
    """Check the host of the configured URL, then seed through it. Nothing connects before the check passed."""
    url = str(get_settings().db.url)
    try:
        host_class = local_host_class(url)
    except NotLocalError as exc:
        raise SystemExit(f"seed-demo refused: {exc}; it writes only to a local database") from None
    print(f"database host: {host_class}")

    db = Database.from_url(url)
    try:
        async with db.session() as session:
            report = await seed(session, today=today or datetime.now(lessons.BILLING_TIME_ZONE).date())
    except DomainError as exc:
        raise SystemExit(f"{exc.code}: {exc}") from None
    finally:
        await db.dispose()

    for line in report.lines():
        print(line)


def main() -> None:
    argparse.ArgumentParser(prog="python -m app.cli.seed_demo", description=__doc__).parse_args()
    asyncio.run(seed_demo())


if __name__ == "__main__":
    main()
