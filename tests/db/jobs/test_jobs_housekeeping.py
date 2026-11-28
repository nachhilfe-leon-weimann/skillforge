"""Housekeeping of the job queue (calendar-categories spec, P0-2; decision H): what goes, what stays, and that it never
waits."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Database
from app.core.db.models import Job, JobStatus
from app.services.jobs.housekeeping import FAILED_RETENTION, SUCCEEDED_RETENTION, delete_finished_jobs

pytestmark = pytest.mark.db

NOW = datetime(2001, 1, 1, tzinfo=UTC)
TICK = timedelta(microseconds=1)


@pytest.fixture(autouse=True)
async def empty_queue(session: AsyncSession) -> AsyncIterator[None]:
    """Clear the rows other tests committed, inside the transaction that rolls back."""
    await session.execute(delete(Job))
    yield


def _job(status: JobStatus, *, finished_at: datetime | None = None, name: str = "") -> Job:
    return Job(
        kind=f"test.housekeeping{name}",
        status=status,
        attempts=1,
        max_attempts=8,
        finished_at=finished_at,
        locked_until=NOW if status is JobStatus.RUNNING else None,
        created_at=NOW - timedelta(days=400),
    )


async def _left(session: AsyncSession) -> set[tuple[JobStatus, datetime | None]]:
    return set((await session.execute(select(Job.status, Job.finished_at))).tuples())


async def test_the_pass_deletes_by_decision_h_and_nothing_else(session: AsyncSession):
    succeeded_cut, failed_cut = NOW - SUCCEEDED_RETENTION, NOW - FAILED_RETENTION
    doomed = [
        _job(JobStatus.SUCCEEDED, finished_at=succeeded_cut - TICK),
        _job(JobStatus.FAILED, finished_at=failed_cut - TICK),
    ]
    kept = [
        _job(JobStatus.SUCCEEDED, finished_at=succeeded_cut),
        _job(JobStatus.FAILED, finished_at=failed_cut),
        # A failed job outlives a succeeded one: past 30 days, within 90.
        _job(JobStatus.FAILED, finished_at=succeeded_cut - timedelta(days=1)),
        # Unfinished jobs are never touched, however old they are.
        _job(JobStatus.PENDING),
        _job(JobStatus.RUNNING),
    ]
    session.add_all([*doomed, *kept])
    await session.flush()

    assert await delete_finished_jobs(session, limit=10, now=NOW) == 2
    assert await _left(session) == {(job.status, job.finished_at) for job in kept}


async def test_the_pass_reads_the_database_clock_by_default(session: AsyncSession):
    session.add_all([_job(JobStatus.SUCCEEDED, finished_at=NOW), _job(JobStatus.FAILED, finished_at=NOW)])
    await session.flush()
    # One finished 31 days ago, one 29 days ago, by the database's clock.
    await session.execute(
        update(Job)
        .where(Job.status == JobStatus.SUCCEEDED)
        .values(finished_at=func.now() - SUCCEEDED_RETENTION - timedelta(days=1))
    )
    await session.execute(
        update(Job)
        .where(Job.status == JobStatus.FAILED)
        .values(finished_at=func.now() - SUCCEEDED_RETENTION + timedelta(days=1))
    )

    assert await delete_finished_jobs(session, limit=10) == 1
    assert {status for status, _ in await _left(session)} == {JobStatus.FAILED}


async def test_a_call_deletes_at_most_limit_jobs_oldest_first(session: AsyncSession):
    finished = [NOW - FAILED_RETENTION - timedelta(days=days) for days in (3, 2, 1)]
    session.add_all(_job(JobStatus.FAILED, finished_at=when) for when in finished)
    await session.flush()

    assert await delete_finished_jobs(session, limit=2, now=NOW) == 2
    assert await _left(session) == {(JobStatus.FAILED, finished[2])}
    assert await delete_finished_jobs(session, limit=2, now=NOW) == 1
    assert await delete_finished_jobs(session, limit=2, now=NOW) == 0


@pytest.fixture
async def committed(db: Database) -> AsyncIterator[None]:
    async with db.session() as cleanup:
        await cleanup.execute(delete(Job))
    try:
        yield
    finally:
        async with db.session() as cleanup:
            await cleanup.execute(delete(Job))


@pytest.mark.usefixtures("committed")
async def test_a_job_another_transaction_holds_is_skipped_not_waited_on(db: Database):
    async with db.session() as setup:
        jobs = [_job(JobStatus.SUCCEEDED, finished_at=NOW - SUCCEEDED_RETENTION - timedelta(days=d)) for d in (2, 1)]
        setup.add_all(jobs)
        await setup.flush()
        held_id = jobs[0].id

    holder = db.session_factory()
    try:
        await holder.execute(select(Job).where(Job.id == held_id).with_for_update())
        async with db.session() as cleaner:
            assert await asyncio.wait_for(delete_finished_jobs(cleaner, limit=10, now=NOW), timeout=5) == 1
        await holder.commit()
    finally:
        await holder.close()

    async with db.session() as cleaner:
        assert await delete_finished_jobs(cleaner, limit=10, now=NOW) == 1
