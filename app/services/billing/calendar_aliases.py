"""The alias table: names as written in the lesson calendar, mapped to students (billing spec, "Calendar import").

A name is stored as the import compares it - normalised by ``calendar_rules.normalize_name`` - so one name maps to
one student, whatever its spelling. An alias holds a name: no message repeats it.
"""

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import CalendarAlias

from .calendar_rules import normalize_name
from .errors import CalendarAliasAlreadyExistsError, CalendarAliasNotFoundError
from .students import require_named_student


async def list_aliases(session: AsyncSession, *, limit: int, offset: int) -> tuple[list[CalendarAlias], int]:
    """Return one page of the aliases by name, plus their number."""
    total = await session.scalar(select(func.count()).select_from(CalendarAlias))
    aliases = await session.scalars(
        select(CalendarAlias).order_by(CalendarAlias.name_normalized).limit(limit).offset(offset)
    )
    return list(aliases), total or 0


async def create_alias(session: AsyncSession, *, name: str, student_id: uuid.UUID) -> CalendarAlias:
    """Map the calendar name, normalised, to the student.

    A name that is already mapped - to this student or another - is ``CalendarAliasAlreadyExistsError``. One
    statement decides, so of two racing requests one stores and the other gets the error, never a unique violation.
    """
    await require_named_student(session, student_id)

    stored = (
        await session.scalars(
            insert(CalendarAlias)
            .values(name_normalized=normalize_name(name), student_party_id=student_id)
            .on_conflict_do_nothing(index_elements=[CalendarAlias.name_normalized])
            .returning(CalendarAlias),
            execution_options={"populate_existing": True},
        )
    ).one_or_none()
    if stored is None:
        raise CalendarAliasAlreadyExistsError("The normalised name is mapped already")

    return stored


async def delete_alias(session: AsyncSession, alias_id: uuid.UUID) -> None:
    """Remove the alias; the name then matches by the students' names again."""
    deleted = await session.scalar(
        delete(CalendarAlias).where(CalendarAlias.id == alias_id).returning(CalendarAlias.id)
    )
    if deleted is None:
        raise CalendarAliasNotFoundError(f"No calendar alias with id {alias_id}")
