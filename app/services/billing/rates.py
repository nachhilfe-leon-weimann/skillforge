"""A student's own rates: each takes effect on its day and holds until the next one (billing spec, decision E).

A rate is addressed by its student and its day. Charges freeze the rate they were priced with (decision F), so
setting or removing a rate here never touches one.
"""

import uuid
from datetime import date

from sqlalchemy import ColumnElement, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import Rate
from app.services.crm.parties import load_party

from .errors import RateNotFoundError, RateNotQuarterHourError
from .students import require_student


async def list_rates(
    session: AsyncSession, student_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[Rate], int]:
    """Return one page of the party's rates, newest day first, plus their number.

    The party must exist, not hold the student role: the rates of a former student stay readable and removable.
    """
    await load_party(session, student_id)

    of_student = Rate.student_party_id == student_id
    total = await session.scalar(select(func.count()).select_from(Rate).where(of_student))
    rates = await session.scalars(
        select(Rate).where(of_student).order_by(Rate.valid_from.desc()).limit(limit).offset(offset)
    )
    return list(rates), total or 0


async def put_rate(
    session: AsyncSession, student_id: uuid.UUID, valid_from: date, *, cents_per_hour: int, note: str | None = None
) -> Rate:
    """Set the student's rate from ``valid_from`` on: create it, or replace amount and note of that day's rate.

    Idempotent: the same amount and note change nothing, not even ``updated_at``. One statement decides, so two
    requests racing for the same day cannot both insert.
    """
    await require_student(session, student_id)
    require_quarter_hour_rate(cents_per_hour)

    values = {"student_party_id": student_id, "valid_from": valid_from, "cents_per_hour": cents_per_hour, "note": note}
    insert_rate = insert(Rate).values(values)
    upsert = insert_rate.on_conflict_do_update(
        constraint="uq_rate_student_valid_from",
        # ON CONFLICT runs no Python-side `onupdate`, so the timestamp is set here.
        set_={"cents_per_hour": cents_per_hour, "note": note, "updated_at": func.now()},
        where=or_(
            Rate.cents_per_hour != insert_rate.excluded.cents_per_hour,
            Rate.note.is_distinct_from(insert_rate.excluded.note),
        ),
    ).returning(Rate)
    written = (await session.scalars(upsert, execution_options={"populate_existing": True})).one_or_none()
    if written is not None:
        return written

    # Nothing differed, so nothing was written: the row is as the request wants it.
    return (await session.scalars(select(Rate).where(*_key(student_id, valid_from)))).one()


async def delete_rate(session: AsyncSession, student_id: uuid.UUID, valid_from: date) -> None:
    """Remove the rate taking effect on ``valid_from``; the rate before it then holds on. Charges keep theirs."""
    await load_party(session, student_id)

    deleted = await session.scalar(delete(Rate).where(*_key(student_id, valid_from)).returning(Rate.id))
    if deleted is None:
        raise RateNotFoundError(f"No rate of party {student_id} on {valid_from}")


def require_quarter_hour_rate(cents_per_hour: int) -> None:
    """Rule G for every rate - a student's and a lesson's own (PD3): checked here, so the database's CHECK never
    answers."""
    if cents_per_hour < 0 or cents_per_hour % 4:
        raise RateNotQuarterHourError(f"{cents_per_hour} cents per hour is not a quarter-hour rate")


def _key(student_id: uuid.UUID, valid_from: date) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    return Rate.student_party_id == student_id, Rate.valid_from == valid_from
