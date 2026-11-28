"""The job queue across real transactions (calendar-categories spec, P0-1; decisions B and C).

These tests commit, so they cannot run on the rolled-back ``session`` fixture; each uses a kind of its own and removes
every job of it. A claim takes any due job, so the fixture clears the whole table before and after.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Database
from app.core.db.models import Job, JobStatus
from app.services.jobs import queue

pytestmark = pytest.mark.db

KEY = "synthetic-key"
# Long enough for a claim that waits on a lock to show it, short enough for a failing test to end.
NO_WAIT = 5


@pytest.fixture
async def kind(db: Database) -> AsyncIterator[str]:
    kind = f"test.race.{uuid.uuid4().hex[:8]}"
    async with db.session() as cleanup:
        await cleanup.execute(delete(Job))
    try:
        yield kind
    finally:
        async with db.session() as cleanup:
            await cleanup.execute(delete(Job))


async def _stored(db: Database, kind: str) -> list[Job]:
    async with db.session(write=False) as check:
        return list(await check.scalars(select(Job).where(Job.kind == kind)))


async def test_two_concurrent_claims_never_wait_for_each_other_and_never_take_the_same_job(db: Database, kind: str):
    async with db.session() as setup:
        for n in range(2):
            await queue.enqueue(setup, kind, {"n": n})

    first, second, third = db.session_factory(), db.session_factory(), db.session_factory()
    try:
        # The first claim holds its job, uncommitted ...
        a = await queue.claim(first)
        # ... and the second takes the other job without waiting for it, the third finds none left.
        b = await asyncio.wait_for(queue.claim(second), timeout=NO_WAIT)
        c = await asyncio.wait_for(queue.claim(third), timeout=NO_WAIT)
        assert a is not None and b is not None
        assert a.id != b.id
        assert c is None
        await first.commit()
        await second.commit()
    finally:
        for session in (first, second, third):
            await session.close()

    assert sorted((job.status, job.attempts) for job in await _stored(db, kind)) == [(JobStatus.RUNNING, 1)] * 2


async def test_racing_claims_hand_out_each_job_once(db: Database, kind: str):
    async with db.session() as setup:
        for n in range(5):
            await queue.enqueue(setup, kind, {"n": n})

    async def claim_and_commit() -> uuid.UUID | None:
        async with db.session() as session:
            claimed = await queue.claim(session)
        return None if claimed is None else claimed.id

    claimed = await asyncio.wait_for(asyncio.gather(*(claim_and_commit() for _ in range(10))), timeout=30)

    ids = [job_id for job_id in claimed if job_id is not None]
    assert len(ids) == len(set(ids)) == 5
    assert {job.attempts for job in await _stored(db, kind)} == {1}


async def _running_job(db: Database, kind: str) -> queue.ClaimedJob:
    """A committed keyed job in its third attempt."""
    async with db.session() as setup:
        await queue.enqueue(setup, kind, {"n": "first"}, dedupe_key=KEY)
    for _ in range(3):
        async with db.session() as session:
            claimed = await queue.claim(session)
            assert claimed is not None
            if claimed.attempts < 3:
                await queue.fail(session, claimed, error="ReadError 503")
                await session.execute(update(Job).where(Job.id == claimed.id).values(available_at=func.now()))
    assert claimed.attempts == 3
    return claimed


type Release = Callable[[AsyncSession, queue.ClaimedJob], Coroutine[None, None, object]]


async def _fail(session: AsyncSession, job: queue.ClaimedJob) -> object:
    return await queue.fail(session, job, error="ReadError 503")


async def _defer(session: AsyncSession, job: queue.ClaimedJob) -> object:
    return await queue.defer(session, job, delay=queue.RETRY_CAP)


@pytest.mark.parametrize(("release", "attempts"), [(_fail, 3), (_defer, 2)], ids=["fail", "defer"])
async def test_a_job_released_while_its_twin_is_being_stored_waits_and_merges_into_it(
    db: Database, kind: str, release: Release, attempts: int
):
    running = await _running_job(db, kind)
    storing, releasing = db.session_factory(), db.session_factory()
    try:
        # The twin is stored but not committed yet ...
        await queue.enqueue(storing, kind, {"n": "twin"}, dedupe_key=KEY)
        # ... when the running job comes back: it waits on the unique index instead of failing on it.
        pending = asyncio.create_task(release(releasing, running))
        await asyncio.sleep(0.5)
        assert not pending.done(), "the release waits for the twin"

        await storing.commit()
        assert await asyncio.wait_for(pending, timeout=10)
        await releasing.commit()
    finally:
        await storing.close()
        await releasing.close()

    assert [(job.payload, job.status, job.attempts) for job in await _stored(db, kind)] == [
        ({"n": "twin"}, JobStatus.PENDING, attempts)
    ]


async def test_a_twin_stored_while_its_job_comes_back_waits_and_stores_nothing(db: Database, kind: str):
    running = await _running_job(db, kind)
    releasing, storing = db.session_factory(), db.session_factory()
    try:
        # The running job is pending again but not committed yet ...
        assert await queue.fail(releasing, running, error="ReadError 503") == JobStatus.PENDING
        # ... when its twin arrives: it waits on the unique index and then finds the job in its place.
        pending = asyncio.create_task(queue.enqueue(storing, kind, {"n": "twin"}, dedupe_key=KEY))
        await asyncio.sleep(0.5)
        assert not pending.done(), "the twin waits for the job"

        await releasing.commit()
        await asyncio.wait_for(pending, timeout=10)
        await storing.commit()
    finally:
        await releasing.close()
        await storing.close()

    assert [(job.id, job.payload, job.status, job.attempts) for job in await _stored(db, kind)] == [
        (running.id, {"n": "first"}, JobStatus.PENDING, 3)
    ]


async def test_two_calls_storing_overlapping_keys_in_opposite_order_wait_instead_of_deadlocking(
    db: Database, kind: str
):
    async def store(session: AsyncSession, *keys: str) -> None:
        jobs = [queue.NewJob({"key": key}, key) for key in keys]
        await queue.enqueue_many(session, kind, jobs, delay=timedelta(seconds=10), max_delay=timedelta(seconds=60))

    first, second = db.session_factory(), db.session_factory()
    try:
        # The first transaction holds "k1" ...
        await store(first, "k1")
        # ... when the second stores "k2" and "k1": in key order it waits on "k1" and holds nothing yet ...
        pending = asyncio.create_task(store(second, "k2", "k1"))
        await asyncio.sleep(0.5)
        assert not pending.done(), "the second call waits for the first"
        # ... so the first can still store "k2"; taken in the given order, "k2" would be the second's: a deadlock.
        await asyncio.wait_for(store(first, "k2"), timeout=NO_WAIT)
        await first.commit()
        await asyncio.wait_for(pending, timeout=10)
        await second.commit()
    finally:
        await first.close()
        await second.close()

    assert sorted((job.dedupe_key, job.status) for job in await _stored(db, kind)) == [
        ("k1", JobStatus.PENDING),
        ("k2", JobStatus.PENDING),
    ]
