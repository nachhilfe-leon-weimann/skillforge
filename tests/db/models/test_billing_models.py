"""The billing models: rule G and the lesson rules in the database, and every party reference guarded by the CRM."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta

import pytest
from asyncpg.exceptions import CheckViolationError, ForeignKeyViolationError, PostgresError, UniqueViolationError
from sqlalchemy import Column, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import CalendarAlias, Lesson, LessonStatus, ManualPayer, Party, PartyType, Rate
from app.core.db.models.base import Base
from app.services.crm.parties import EXTERNAL_LINKS

START = datetime(2026, 11, 2, 15, tzinfo=UTC)


def _lesson(party_id: uuid.UUID) -> Lesson:
    return Lesson(student_party_id=party_id, subject="Mathe", starts_at=START, minutes=60)


def _rate(party_id: uuid.UUID) -> Rate:
    return Rate(student_party_id=party_id, cents_per_hour=3000, valid_from=date(2026, 11, 1))


def _party_references(schema: str) -> set[Column]:
    """Every column of ``schema`` with a foreign key into ``core.party``."""
    party = Base.metadata.tables["core.party"]
    return {
        foreign_key.parent
        for table in Base.metadata.tables.values()
        if table.schema == schema
        for foreign_key in table.foreign_keys
        if foreign_key.column.table is party
    }


@pytest.mark.parametrize("schema", ["billing", "ext"])
def test_every_party_reference_of_billing_and_ext_has_its_external_link(schema: str):
    """A new column referencing a party without its ``EXTERNAL_LINKS`` row fails here (billing spec, standing
    criteria): the delete would otherwise cascade it away or hit ``RESTRICT`` as a 500."""
    guarded = {link.party_column.expression for link in EXTERNAL_LINKS}

    references = _party_references(schema)

    assert references
    assert {f"{column.table.name}.{column.name}" for column in references - guarded} == set()


def test_the_billing_party_references_are_lesson_and_rate_and_restrict():
    references = _party_references("billing")

    assert {f"{column.table.name}.{column.name}" for column in references} == {
        "lesson.student_party_id",
        "rate.student_party_id",
        "lesson.payer_party_id",
        "manual_payer.party_id",
        "calendar_alias.student_party_id",
    }
    assert {foreign_key.ondelete for column in references for foreign_key in column.foreign_keys} == {"RESTRICT"}


def test_the_link_kinds_are_unique():
    kinds = [link.kind for link in EXTERNAL_LINKS]

    assert len(kinds) == len(set(kinds))


async def _party(session: AsyncSession) -> uuid.UUID:
    party = Party(type=PartyType.PERSON)
    session.add(party)
    await session.flush()
    return party.id


async def _refused(session: AsyncSession, row: object) -> tuple[type[PostgresError], str | None]:
    """Add ``row`` inside a savepoint and return the driver error that refused it with its constraint's name.

    SQLAlchemy's asyncpg dialect wraps the driver error; the raw ``PostgresError`` is the ``__cause__`` of ``orig``.
    """
    with pytest.raises(IntegrityError) as refused:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    cause = refused.value.orig.__cause__ if refused.value.orig is not None else None
    assert isinstance(cause, PostgresError)
    return type(cause), getattr(cause, "constraint_name", None)


@pytest.mark.db
async def test_a_lesson_and_a_rate_store_with_their_defaults(session: AsyncSession):
    party_id = await _party(session)
    lesson = Lesson(student_party_id=party_id, subject="Mathe", starts_at=START, minutes=45)
    rate = Rate(student_party_id=party_id, cents_per_hour=0, valid_from=date(2026, 11, 1))
    session.add_all([lesson, rate])
    await session.flush()
    await session.refresh(lesson)

    assert (lesson.status, lesson.billable, lesson.rate_cents_per_hour) == (LessonStatus.PLANNED, False, None)
    assert rate.cents_per_hour == 0


@pytest.mark.db
@pytest.mark.parametrize(
    ("minutes", "rate_cents_per_hour"),
    [(15, 0), (45, None), (60, 4), (120, 3204)],
    ids=["15-min-own-rate-0", "45-min-no-own-rate", "60-min-own-rate-4", "120-min-own-rate-3204"],
)
async def test_the_database_accepts_a_lesson_on_the_quarter_hour(
    session: AsyncSession, minutes: int, rate_cents_per_hour: int | None
):
    """The checks are no stricter than rule G: a 15-minute lesson exists (SP-CAL), and an own rate may be 0 (PD3)."""
    party_id = await _party(session)
    lesson = Lesson(
        student_party_id=party_id,
        subject="Mathe",
        starts_at=START,
        minutes=minutes,
        rate_cents_per_hour=rate_cents_per_hour,
    )
    session.add(lesson)

    await session.flush()

    assert (lesson.minutes, lesson.rate_cents_per_hour) == (minutes, rate_cents_per_hour)


@pytest.mark.db
@pytest.mark.parametrize("cents_per_hour", [0, 4, 3204])
async def test_the_database_accepts_a_rate_on_the_quarter_hour(session: AsyncSession, cents_per_hour: int):
    party_id = await _party(session)
    rate = Rate(student_party_id=party_id, cents_per_hour=cents_per_hour, valid_from=date(2026, 11, 1))
    session.add(rate)

    await session.flush()

    assert rate.cents_per_hour == cents_per_hour


@pytest.mark.db
@pytest.mark.parametrize("cents_per_hour", [3001, 3002, 3003, -4])
async def test_the_database_refuses_a_rate_off_the_quarter_hour(session: AsyncSession, cents_per_hour: int):
    party_id = await _party(session)

    rate = Rate(student_party_id=party_id, cents_per_hour=cents_per_hour, valid_from=date(2026, 11, 1))

    assert await _refused(session, rate) == (CheckViolationError, "ck_rate_quarter_hour_cents")


@pytest.mark.db
@pytest.mark.parametrize("minutes", [0, 10, 50, 61, -15])
async def test_the_database_refuses_a_lesson_off_the_quarter_hour(session: AsyncSession, minutes: int):
    party_id = await _party(session)

    lesson = Lesson(student_party_id=party_id, subject="Mathe", starts_at=START, minutes=minutes)

    assert await _refused(session, lesson) == (CheckViolationError, "ck_lesson_quarter_hour_minutes")


@pytest.mark.db
@pytest.mark.parametrize("rate_cents_per_hour", [3001, 3002, -4])
async def test_the_database_refuses_a_lesson_rate_off_the_quarter_hour(session: AsyncSession, rate_cents_per_hour: int):
    party_id = await _party(session)

    lesson = Lesson(
        student_party_id=party_id,
        subject="Mathe",
        starts_at=START,
        minutes=60,
        rate_cents_per_hour=rate_cents_per_hour,
    )

    assert await _refused(session, lesson) == (CheckViolationError, "ck_lesson_quarter_hour_cents")


@pytest.mark.db
@pytest.mark.parametrize("status", [LessonStatus.PLANNED, LessonStatus.NOT_HELD])
async def test_the_database_refuses_a_billable_lesson_that_was_not_held_or_cancelled(
    session: AsyncSession, status: LessonStatus
):
    party_id = await _party(session)

    lesson = Lesson(
        student_party_id=party_id, subject="Mathe", starts_at=START, minutes=60, status=status, billable=True
    )

    assert await _refused(session, lesson) == (CheckViolationError, "ck_lesson_billable_status")


@pytest.mark.db
@pytest.mark.parametrize("status", [LessonStatus.HELD, LessonStatus.CANCELLED])
async def test_a_held_or_cancelled_lesson_can_be_billable(session: AsyncSession, status: LessonStatus):
    party_id = await _party(session)
    session.add(
        Lesson(student_party_id=party_id, subject="Mathe", starts_at=START, minutes=60, status=status, billable=True)
    )

    await session.flush()


@pytest.mark.db
async def test_a_student_has_one_lesson_per_start_and_one_rate_per_day(session: AsyncSession):
    party_id = await _party(session)
    session.add_all([
        Lesson(student_party_id=party_id, subject="Mathe", starts_at=START, minutes=60),
        Rate(student_party_id=party_id, cents_per_hour=3000, valid_from=date(2026, 11, 1)),
    ])
    await session.flush()

    lesson = Lesson(student_party_id=party_id, subject="Physik", starts_at=START, minutes=45)
    rate = Rate(student_party_id=party_id, cents_per_hour=3200, valid_from=date(2026, 11, 1))

    assert await _refused(session, lesson) == (UniqueViolationError, "uq_lesson_student_start")
    assert await _refused(session, rate) == (UniqueViolationError, "uq_rate_student_valid_from")


@pytest.mark.db
@pytest.mark.parametrize("billing_row", [_lesson, _rate])
async def test_the_database_refuses_to_delete_a_party_with_a_billing_row(
    session: AsyncSession, billing_row: Callable[[uuid.UUID], Lesson | Rate]
):
    """``RESTRICT`` is the backstop behind the CRM's guard: a party never takes its lessons or rates along."""
    party_id = await _party(session)
    session.add(billing_row(party_id))
    await session.flush()

    with pytest.raises(IntegrityError) as refused:
        async with session.begin_nested():
            await session.execute(delete(Party).where(Party.id == party_id))

    orig = refused.value.orig
    assert orig is not None and isinstance(orig.__cause__, ForeignKeyViolationError)
    assert await session.get(Party, party_id) is not None


async def _company(session: AsyncSession) -> uuid.UUID:
    party = Party(type=PartyType.COMPANY)
    session.add(party)
    await session.flush()
    return party.id


@pytest.mark.db
async def test_a_lesson_stores_without_a_payer_and_with_a_company_as_payer(session: AsyncSession):
    """The payer is empty until the lesson becomes billable, and an authority that pays is a company (decision I)."""
    student_id, company_id = await _party(session), await _company(session)
    without = Lesson(student_party_id=student_id, subject="Mathe", starts_at=START, minutes=60)
    with_company = Lesson(
        student_party_id=student_id,
        subject="Mathe",
        starts_at=START + timedelta(days=7),
        minutes=60,
        status=LessonStatus.HELD,
        billable=True,
        payer_party_id=company_id,
    )
    session.add_all([without, with_company, ManualPayer(party_id=company_id)])
    await session.flush()

    assert (without.payer_party_id, with_company.payer_party_id) == (None, company_id)
    assert (await session.get(ManualPayer, company_id)) is not None


@pytest.mark.db
async def test_a_party_is_marked_as_billed_by_hand_once(session: AsyncSession):
    party_id = await _party(session)
    session.add(ManualPayer(party_id=party_id, note="by hand"))
    await session.flush()

    assert await _refused(session, ManualPayer(party_id=party_id)) == (UniqueViolationError, "manual_payer_pkey")


async def _paid_lesson(session: AsyncSession, payer_id: uuid.UUID) -> Lesson:
    """A lesson of another student that names the party as its payer."""
    lesson = _lesson(await _party(session))
    lesson.payer_party_id = payer_id
    return lesson


async def _manual_payer(session: AsyncSession, party_id: uuid.UUID) -> ManualPayer:
    return ManualPayer(party_id=party_id)


@pytest.mark.db
@pytest.mark.parametrize(
    ("payer_row", "constraint"),
    [(_paid_lesson, "lesson_payer_party_id_fkey"), (_manual_payer, "manual_payer_party_id_fkey")],
    ids=["lesson_payer", "manual_payer"],
)
async def test_the_database_refuses_to_delete_a_payer(
    session: AsyncSession,
    payer_row: Callable[[AsyncSession, uuid.UUID], Awaitable[Lesson | ManualPayer]],
    constraint: str,
):
    """``RESTRICT`` behind the CRM's guard: a payer never takes its lessons or its mark along."""
    party_id = await _party(session)
    session.add(await payer_row(session, party_id))
    await session.flush()

    with pytest.raises(IntegrityError) as refused:
        async with session.begin_nested():
            await session.execute(delete(Party).where(Party.id == party_id))

    orig = refused.value.orig
    assert orig is not None and isinstance(orig.__cause__, ForeignKeyViolationError)
    assert getattr(orig.__cause__, "constraint_name", None) == constraint
    assert await session.get(Party, party_id) is not None


@pytest.mark.db
async def test_a_calendar_name_maps_to_one_student_who_cannot_be_deleted_under_it(session: AsyncSession):
    """One normalised name, one alias; ``RESTRICT`` behind the CRM's guard keeps the student the alias names."""
    party_id, other_id = await _party(session), await _party(session)
    session.add(CalendarAlias(student_party_id=party_id, name_normalized="student b"))
    await session.flush()

    second = CalendarAlias(student_party_id=other_id, name_normalized="student b")
    assert await _refused(session, second) == (UniqueViolationError, "calendar_alias_name_normalized_key")

    with pytest.raises(IntegrityError) as refused:
        async with session.begin_nested():
            await session.execute(delete(Party).where(Party.id == party_id))
    orig = refused.value.orig
    assert orig is not None and isinstance(orig.__cause__, ForeignKeyViolationError)
    assert getattr(orig.__cause__, "constraint_name", None) == "calendar_alias_student_party_id_fkey"
