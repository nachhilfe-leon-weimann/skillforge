from collections.abc import AsyncGenerator, Collection
from contextlib import asynccontextmanager

from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import StudentSubject, Subject, TutorSubject

from .errors import SubjectAlreadyExistsError, SubjectInUseError, SubjectNotFoundError, UnknownSubjectError


async def list_subjects(session: AsyncSession, *, limit: int, offset: int) -> tuple[list[Subject], int]:
    """Return one page of subjects ordered by title (case-insensitively) plus the total count."""
    total = await session.scalar(select(func.count()).select_from(Subject))
    result = await session.execute(
        select(Subject).order_by(func.lower(Subject.title), Subject.id).limit(limit).offset(offset)
    )
    return list(result.scalars()), total or 0


async def get_subject(session: AsyncSession, subject_id: int) -> Subject:
    subject = await session.get(Subject, subject_id)
    if subject is None:
        raise SubjectNotFoundError(f"No subject with id {subject_id}")

    return subject


async def require_subjects(session: AsyncSession, subject_ids: Collection[int]) -> None:
    """Raise ``UnknownSubjectError`` naming every ID in ``subject_ids`` that is not a subject."""
    if not subject_ids:
        return

    known = await session.scalars(select(Subject.id).where(Subject.id.in_(subject_ids)))
    unknown = sorted(set(subject_ids) - set(known))
    if unknown:
        raise UnknownSubjectError(f"{UnknownSubjectError.message}: {', '.join(map(str, unknown))}")


async def create_subject(session: AsyncSession, *, title: str) -> Subject:
    subject = Subject(title=title)
    async with _unique_title(session):
        session.add(subject)
    return subject


async def update_subject(session: AsyncSession, subject_id: int, *, title: str | None = None) -> Subject:
    """Change the given fields; ``None`` means unchanged (the column is ``NOT NULL``)."""
    subject = await get_subject(session, subject_id)
    if title is not None:
        async with _unique_title(session):
            subject.title = title

    return subject


async def delete_subject(session: AsyncSession, subject_id: int) -> None:
    subject = await get_subject(session, subject_id)
    # Both association tables cascade on delete, so the database would not refuse: check explicitly.
    in_use = await session.scalar(
        select(
            exists().where(StudentSubject.subject_id == subject_id)
            | exists().where(TutorSubject.subject_id == subject_id)
        )
    )
    if in_use:
        raise SubjectInUseError(f"Subject {subject_id} is still assigned")

    await session.delete(subject)
    await session.flush()


@asynccontextmanager
async def _unique_title(session: AsyncSession) -> AsyncGenerator[None]:
    """Write the change made inside the block in a SAVEPOINT and translate a title conflict.

    Uniqueness is decided by uq_subject_title_lower, not by check-then-insert; the flush forces the
    violation here instead of at commit. The change must happen *inside* the block:
    ``begin_nested()`` first flushes whatever is pending into the enclosing transaction, so a change
    made before it would fail out there and take the whole transaction down.
    """
    try:
        async with session.begin_nested():
            yield
            await session.flush()
    except IntegrityError as exc:
        raise SubjectAlreadyExistsError("Subject title is already taken") from exc
