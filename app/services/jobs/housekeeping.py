"""Housekeeping of the job queue (calendar-categories spec, decision H): deleting finished jobs.

A ``succeeded`` job goes ``SUCCEEDED_RETENTION`` after its ``finished_at``, a ``failed`` one - the evidence of what
went wrong - ``FAILED_RETENTION`` after it. A ``pending`` or ``running`` job is never touched.
"""

from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import CursorResult, and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import Job, JobStatus

SUCCEEDED_RETENTION = timedelta(days=30)
FAILED_RETENTION = timedelta(days=90)


async def delete_finished_jobs(session: AsyncSession, *, limit: int, now: datetime | None = None) -> int:
    """Delete up to ``limit`` finished jobs past their retention, oldest first.

    ``now`` defaults to the database's clock, the one ``finished_at`` was stamped with. Rows another transaction
    holds are skipped, never waited on: they go in a later batch.
    """
    reference = func.now() if now is None else now
    expired = or_(
        and_(Job.status == JobStatus.SUCCEEDED, Job.finished_at < reference - SUCCEEDED_RETENTION),
        and_(Job.status == JobStatus.FAILED, Job.finished_at < reference - FAILED_RETENTION),
    )
    doomed = select(Job.id).where(expired).order_by(Job.finished_at).limit(limit).with_for_update(skip_locked=True)
    result = await session.execute(delete(Job).where(Job.id.in_(doomed)))
    # ``execute`` is typed as ``Result``; a DELETE always yields a ``CursorResult`` with ``rowcount``.
    return cast(CursorResult, result).rowcount
