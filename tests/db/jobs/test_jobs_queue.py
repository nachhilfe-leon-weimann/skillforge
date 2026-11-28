"""The job queue in one transaction (calendar-categories spec, P0-1; decisions B, C, D, E and G).

``now()`` stands still within a transaction, so every due time is asserted exactly against it; a call made at an
earlier time is set up by moving the stored times back.
"""

import re
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import Job, JobStatus
from app.services.jobs import queue

pytestmark = pytest.mark.db

KIND = "test.kind"
KEY = "synthetic-key"


@pytest.fixture(autouse=True)
async def empty_queue(session: AsyncSession) -> AsyncIterator[None]:
    """A claim takes any due job: clear the rows other tests committed, inside the transaction that rolls back."""
    await session.execute(delete(Job))
    yield


async def _jobs(session: AsyncSession) -> list[Job]:
    result = await session.scalars(select(Job).where(Job.kind == KIND).execution_options(populate_existing=True))
    return list(result)


async def _one(session: AsyncSession) -> Job:
    (job,) = await _jobs(session)
    return job


async def _now(session: AsyncSession):
    return await session.scalar(select(func.now()))


async def _shift(session: AsyncSession, by: timedelta, **where) -> None:
    """Move a job's stored times back by ``by``, as if it had been stored that much earlier."""
    await session.execute(
        update(Job)
        .filter_by(kind=KIND, **where)
        .values(created_at=Job.created_at - by, available_at=Job.available_at - by)
    )


async def _make_due(session: AsyncSession) -> None:
    await session.execute(update(Job).where(Job.kind == KIND).values(available_at=func.now()))


async def _expire_lease(session: AsyncSession) -> None:
    await session.execute(
        update(Job).where(Job.kind == KIND, Job.status == JobStatus.RUNNING).values(locked_until=func.now())
    )


async def _claim(session: AsyncSession) -> queue.ClaimedJob:
    claimed = await queue.claim(session)
    assert claimed is not None
    return claimed


# --- enqueue (decisions C and D) ---


async def test_enqueue_stores_a_pending_job_due_after_its_delay(session: AsyncSession):
    await queue.enqueue(session, KIND, {"n": 1}, delay=timedelta(seconds=30))

    job = await _one(session)
    assert (job.status, job.payload, job.attempts, job.max_attempts) == (
        JobStatus.PENDING,
        {"n": 1},
        0,
        queue.DEFAULT_MAX_ATTEMPTS,
    )
    assert job.available_at == await _now(session) + timedelta(seconds=30)
    assert await queue.claim(session) is None, "not due yet"


async def test_fifty_enqueues_with_one_key_store_one_job_and_one_while_it_runs_stores_one_more(session: AsyncSession):
    for _ in range(50):
        await queue.enqueue(session, KIND, {"n": 1}, dedupe_key=KEY)
    assert len(await _jobs(session)) == 1

    await _claim(session)
    await queue.enqueue(session, KIND, {"n": 2}, dedupe_key=KEY)
    await queue.enqueue(session, KIND, {"n": 3}, dedupe_key=KEY)

    jobs = await _jobs(session)
    assert sorted((job.status, job.payload["n"]) for job in jobs) == [(JobStatus.PENDING, 2), (JobStatus.RUNNING, 1)]


async def test_jobs_without_a_key_never_conflict(session: AsyncSession):
    for _ in range(3):
        await queue.enqueue(session, KIND)

    assert len(await _jobs(session)) == 3


async def test_a_conflict_without_max_delay_leaves_the_pending_job_as_it_is(session: AsyncSession):
    await queue.enqueue(session, KIND, dedupe_key=KEY, delay=timedelta(seconds=10))
    await _shift(session, timedelta(seconds=5))
    before = (await _one(session)).available_at

    await queue.enqueue(session, KIND, dedupe_key=KEY, delay=timedelta(seconds=10))

    assert (await _one(session)).available_at == before


async def test_debounce_moves_the_due_time_but_never_past_max_delay_after_the_first_call(session: AsyncSession):
    delay, max_delay = timedelta(seconds=10), timedelta(seconds=60)
    await queue.enqueue(session, KIND, dedupe_key=KEY, delay=delay, max_delay=max_delay)
    now = await _now(session)
    assert (await _one(session)).available_at == now + delay

    async def call_after(elapsed: int):
        """A further call ``elapsed`` seconds after the first one: the due time before it, and after it."""
        first = now - timedelta(seconds=elapsed)
        await session.execute(update(Job).where(Job.kind == KIND).values(created_at=first, available_at=first + delay))
        await queue.enqueue(session, KIND, dedupe_key=KEY, delay=delay, max_delay=max_delay)
        return first + delay, (await _one(session)).available_at

    # A further call moves the due time to 10 s after itself ...
    for elapsed in (5, 20, 50):
        before, after = await call_after(elapsed)
        assert before < after == now + delay, elapsed
    # ... unless that lies past 60 s after the first call: then the due time is that bound.
    for elapsed in (55, 59):
        before, after = await call_after(elapsed)
        assert before < after == now - timedelta(seconds=elapsed) + max_delay < now + delay, elapsed

    assert len(await _jobs(session)) == 1


async def test_debounce_never_cuts_a_backoff_short(session: AsyncSession):
    """A job that failed waits out its backoff: a further call for its key must not make it due at once."""
    delay, max_delay = timedelta(seconds=10), timedelta(seconds=60)
    await queue.enqueue(session, KIND, dedupe_key=KEY, delay=delay, max_delay=max_delay)
    await _make_due(session)
    job = await _claim(session)
    await queue.fail(session, job, error="Synthetic", retry_after=timedelta(minutes=30))
    # The job was first stored long ago: `created_at + max_delay` lies in the past.
    await session.execute(update(Job).where(Job.kind == KIND).values(created_at=Job.created_at - timedelta(hours=1)))
    backoff = (await _one(session)).available_at

    await queue.enqueue(session, KIND, dedupe_key=KEY, delay=delay, max_delay=max_delay)

    assert (await _one(session)).available_at == backoff == await _now(session) + timedelta(minutes=30)


async def test_enqueue_many_stores_a_repeated_key_once_with_its_first_payload(session: AsyncSession):
    jobs = [queue.NewJob({"n": n}, key) for n, key in enumerate(("b", "a", "b", "a", "c"))]

    await queue.enqueue_many(session, KIND, jobs, delay=timedelta(seconds=10), max_delay=timedelta(seconds=60))

    assert sorted((job.dedupe_key, job.payload["n"]) for job in await _jobs(session)) == [("a", 1), ("b", 0), ("c", 4)]


async def test_enqueue_many_follows_decisions_c_and_d_per_key(session: AsyncSession):
    delay, max_delay = timedelta(seconds=10), timedelta(seconds=60)
    await queue.enqueue_many(session, KIND, [queue.NewJob({"n": 0}, "a"), queue.NewJob({"n": 0}, "b")], delay=delay)
    now = await _now(session)
    # "a" was first stored 55 s ago, "b" 20 s ago; "c" is new.
    for key, elapsed in (("a", 55), ("b", 20)):
        first = now - timedelta(seconds=elapsed)
        await session.execute(
            update(Job).where(Job.dedupe_key == key).values(created_at=first, available_at=first + delay)
        )

    keys = ("c", "b", "a")
    await queue.enqueue_many(
        session, KIND, [queue.NewJob({"n": 1}, key) for key in keys], delay=delay, max_delay=max_delay
    )

    stored = sorted((job.dedupe_key, job.payload["n"], job.available_at) for job in await _jobs(session))
    assert stored == [
        ("a", 0, now - timedelta(seconds=55) + max_delay),
        ("b", 0, now + delay),
        ("c", 1, now + delay),
    ]


async def test_enqueue_many_without_jobs_sends_no_statement(session: AsyncSession, statements: list[str]):
    statements.clear()

    await queue.enqueue_many(session, KIND, [], max_delay=timedelta(seconds=60))

    assert statements == []


@pytest.mark.parametrize(
    ("dedupe_key", "delay", "max_delay", "message"),
    [
        (KEY, timedelta(seconds=10), timedelta(seconds=5), "max_delay must not be shorter than delay"),
        (None, timedelta(seconds=10), timedelta(seconds=60), "max_delay needs a dedupe_key"),
    ],
)
async def test_enqueue_refuses_a_debounce_that_cannot_work(
    session: AsyncSession, dedupe_key: str | None, delay: timedelta, max_delay: timedelta, message: str
):
    with pytest.raises(ValueError, match=message):
        await queue.enqueue(session, KIND, dedupe_key=dedupe_key, delay=delay, max_delay=max_delay)

    assert await _jobs(session) == []


# --- claim (decision B) ---


async def test_a_claim_takes_the_due_job_counts_the_attempt_and_sets_the_lease(session: AsyncSession):
    await queue.enqueue(session, KIND, {"n": 1})

    claimed = await _claim(session)

    job = await _one(session)
    assert claimed == queue.ClaimedJob(job.id, KIND, {"n": 1}, 1, queue.DEFAULT_MAX_ATTEMPTS, JobStatus.RUNNING)
    assert (job.status, job.attempts) == (JobStatus.RUNNING, 1)
    assert job.locked_until == await _now(session) + queue.JOB_LEASE
    assert await queue.claim(session) is None


async def test_a_running_job_is_claimed_again_only_after_its_lease_and_that_counts_as_an_attempt(
    session: AsyncSession,
):
    await queue.enqueue(session, KIND)
    first = await _claim(session)
    await session.execute(update(Job).where(Job.kind == KIND).values(locked_until=func.now() + timedelta(seconds=1)))
    assert await queue.claim(session) is None, "the lease has not passed"

    await _expire_lease(session)
    again = await _claim(session)

    assert (again.id, again.attempts, again.runnable) == (first.id, 2, True)
    assert (await _one(session)).locked_until == await _now(session) + queue.JOB_LEASE


async def test_a_running_job_without_an_attempt_left_is_finished_as_failed_by_the_claim(session: AsyncSession):
    await queue.enqueue(session, KIND, max_attempts=2)
    await _claim(session)
    await _expire_lease(session)
    await _claim(session)
    await _expire_lease(session)

    finished = await _claim(session)

    assert (finished.runnable, finished.status, finished.attempts) == (False, JobStatus.FAILED, 2)
    job = await _one(session)
    assert (job.status, job.attempts, job.locked_until, job.last_error) == (
        JobStatus.FAILED,
        2,
        None,
        queue.LEASE_EXPIRED,
    )
    assert job.finished_at == await _now(session)
    assert await queue.claim(session) is None


async def test_the_claim_takes_the_job_due_first(session: AsyncSession):
    await queue.enqueue(session, KIND, {"n": "later"})
    await queue.enqueue(session, KIND, {"n": "earlier"})
    await _shift(session, timedelta(seconds=5), payload={"n": "earlier"})

    assert (await _claim(session)).payload == {"n": "earlier"}


# --- succeed and a stale holder (decision B) ---


async def test_succeed_finishes_the_claimed_job(session: AsyncSession):
    await queue.enqueue(session, KIND)
    claimed = await _claim(session)

    assert await queue.succeed(session, claimed) is True

    job = await _one(session)
    assert (job.status, job.locked_until, job.finished_at) == (JobStatus.SUCCEEDED, None, await _now(session))
    assert await queue.succeed(session, claimed) is False, "a finished job is not finished twice"


async def test_a_stale_holder_changes_nothing(session: AsyncSession):
    await queue.enqueue(session, KIND, {"n": 1}, dedupe_key=KEY)
    stale = await _claim(session)
    await _expire_lease(session)
    current = await _claim(session)

    assert await queue.succeed(session, stale) is False
    assert await queue.fail(session, stale, error="SomeError 500") is None
    assert await queue.defer(session, stale, delay=timedelta(minutes=5)) is False

    job = await _one(session)
    assert (job.id, job.status, job.attempts, job.last_error) == (current.id, JobStatus.RUNNING, 2, None)
    assert job.locked_until == await _now(session) + queue.JOB_LEASE


async def test_a_stale_holder_cannot_overturn_the_failure_its_lease_ended_in(session: AsyncSession):
    await queue.enqueue(session, KIND, max_attempts=1)
    stale = await _claim(session)
    await _expire_lease(session)
    assert not (await _claim(session)).runnable

    assert await queue.succeed(session, stale) is False
    assert (await _one(session)).status == JobStatus.FAILED


# --- fail (decision E) ---


@pytest.mark.parametrize(
    ("attempts", "minutes"), [(1, 1), (2, 2), (3, 4), (4, 8), (5, 16), (6, 32), (7, 60), (8, 60), (500, 60)]
)
def test_the_retry_delay_doubles_per_attempt_up_to_the_cap(attempts: int, minutes: int):
    assert queue.retry_delay(attempts) == timedelta(minutes=minutes)


@pytest.mark.parametrize(
    ("attempts", "retry_after", "expected"),
    [
        (1, timedelta(minutes=5), timedelta(minutes=5)),
        (3, timedelta(minutes=1), timedelta(minutes=4)),
        (2, timedelta(hours=3), queue.RETRY_CAP),
    ],
)
def test_a_longer_retry_after_wins_up_to_the_cap(attempts: int, retry_after: timedelta, expected: timedelta):
    assert queue.retry_delay(attempts, retry_after) == expected


async def test_fail_makes_the_job_due_after_the_backoff_and_records_the_error(session: AsyncSession):
    await queue.enqueue(session, KIND, {"n": 1})
    for attempt, delay in ((1, timedelta(minutes=1)), (2, timedelta(minutes=2)), (3, timedelta(minutes=4))):
        claimed = await _claim(session)
        assert claimed.attempts == attempt

        assert await queue.fail(session, claimed, error=f"ReadError {500 + attempt}") == JobStatus.PENDING

        job = await _one(session)
        assert (job.id, job.status, job.attempts, job.locked_until, job.last_error, job.payload) == (
            claimed.id,
            JobStatus.PENDING,
            attempt,
            None,
            f"ReadError {500 + attempt}",
            {"n": 1},
        )
        assert job.available_at == await _now(session) + delay
        await _make_due(session)


async def test_fail_honours_a_longer_retry_after_up_to_the_cap(session: AsyncSession):
    await queue.enqueue(session, KIND)

    await queue.fail(session, await _claim(session), error="Throttled 429", retry_after=timedelta(minutes=7))
    assert (await _one(session)).available_at == await _now(session) + timedelta(minutes=7)

    await _make_due(session)
    await queue.fail(session, await _claim(session), error="Throttled 429", retry_after=timedelta(hours=2))
    assert (await _one(session)).available_at == await _now(session) + queue.RETRY_CAP


async def test_fail_after_the_last_attempt_finishes_the_job_as_failed(session: AsyncSession):
    await queue.enqueue(session, KIND, max_attempts=2)
    assert await queue.fail(session, await _claim(session), error="ReadError 503") == JobStatus.PENDING
    await _make_due(session)

    assert await queue.fail(session, await _claim(session), error="ReadError 504") == JobStatus.FAILED

    job = await _one(session)
    assert (job.status, job.attempts, job.locked_until, job.last_error) == (JobStatus.FAILED, 2, None, "ReadError 504")
    assert job.finished_at == await _now(session)
    await _make_due(session)
    assert await queue.claim(session) is None


# --- the pending twin (decision C) ---


async def _claim_attempt(session: AsyncSession, attempt: int) -> queue.ClaimedJob:
    """Enqueue a keyed job and claim it until its ``attempt``-th attempt runs."""
    await queue.enqueue(session, KIND, {"n": "first"}, dedupe_key=KEY)
    claimed = await _claim(session)
    while claimed.attempts < attempt:
        await queue.fail(session, claimed, error="ReadError 503")
        await _make_due(session)
        claimed = await _claim(session)
    return claimed


@pytest.mark.parametrize(
    ("retry_after", "due_in"), [(None, timedelta(minutes=4)), (timedelta(minutes=30), timedelta(minutes=30))]
)
async def test_a_job_failing_beside_a_pending_twin_is_deleted_and_the_twin_takes_its_attempts_and_backoff(
    session: AsyncSession, retry_after: timedelta | None, due_in: timedelta
):
    running = await _claim_attempt(session, 3)
    await queue.enqueue(session, KIND, {"n": "twin"}, dedupe_key=KEY, delay=timedelta(seconds=10))
    (twin_id,) = [job.id for job in await _jobs(session) if job.status == JobStatus.PENDING]

    assert await queue.fail(session, running, error="ReadError 503", retry_after=retry_after) == JobStatus.PENDING

    job = await _one(session)
    assert (job.id, job.payload, job.status, job.attempts) == (twin_id, {"n": "twin"}, JobStatus.PENDING, 3)
    assert job.available_at == await _now(session) + due_in, "the twin waits out the failed job's backoff"


async def test_a_twin_keeps_its_attempts_and_due_time_when_they_are_greater(session: AsyncSession):
    running = await _claim_attempt(session, 2)
    await queue.enqueue(session, KIND, {"n": "twin"}, dedupe_key=KEY, delay=queue.RETRY_CAP)
    await session.execute(update(Job).where(Job.status == JobStatus.PENDING, Job.kind == KIND).values(attempts=5))

    await queue.fail(session, running, error="ReadError 503")

    job = await _one(session)
    assert (job.payload, job.attempts, job.available_at) == ({"n": "twin"}, 5, await _now(session) + queue.RETRY_CAP)


async def test_a_twin_with_fewer_max_attempts_takes_the_greater_one(session: AsyncSession):
    """Otherwise the merged job would hold more attempts than it may make - a pending job without one left."""
    running = await _claim_attempt(session, 3)
    await queue.enqueue(session, KIND, {"n": "twin"}, dedupe_key=KEY, max_attempts=2)

    await queue.fail(session, running, error="ReadError 503")

    job = await _one(session)
    assert (job.payload, job.attempts, job.max_attempts) == ({"n": "twin"}, 3, queue.DEFAULT_MAX_ATTEMPTS)
    await _make_due(session)
    assert (await _claim(session)).runnable


async def test_a_job_failing_for_good_leaves_its_twin_alone(session: AsyncSession):
    await queue.enqueue(session, KIND, {"n": "first"}, dedupe_key=KEY, max_attempts=1)
    running = await _claim(session)
    await queue.enqueue(session, KIND, {"n": "twin"}, dedupe_key=KEY)

    assert await queue.fail(session, running, error="ReadError 503") == JobStatus.FAILED

    jobs = await _jobs(session)
    assert sorted((job.payload["n"], job.status, job.attempts) for job in jobs) == [
        ("first", JobStatus.FAILED, 1),
        ("twin", JobStatus.PENDING, 0),
    ]


# --- defer (decision G) ---


async def test_defer_makes_the_job_due_again_without_counting_the_attempt(session: AsyncSession):
    claimed = await _claim_attempt(session, 2)

    assert await queue.defer(session, claimed, delay=queue.RETRY_CAP) is True

    job = await _one(session)
    assert (job.id, job.status, job.attempts, job.locked_until, job.last_error) == (
        claimed.id,
        JobStatus.PENDING,
        1,
        None,
        "ReadError 503",
    )
    assert job.available_at == await _now(session) + queue.RETRY_CAP


async def test_defer_beside_a_pending_twin_deletes_the_job_and_the_twin_takes_its_attempts(session: AsyncSession):
    claimed = await _claim_attempt(session, 3)
    await queue.enqueue(session, KIND, {"n": "twin"}, dedupe_key=KEY)

    assert await queue.defer(session, claimed, delay=queue.RETRY_CAP) is True

    job = await _one(session)
    assert (job.payload, job.status, job.attempts) == ({"n": "twin"}, JobStatus.PENDING, 2)


# --- the claim's plan ---


async def test_the_claim_reads_the_partial_indexes_and_never_the_finished_jobs(session: AsyncSession):
    """Each branch of the claim has its partial index, so the claim reads no finished job. With sequential scans
    priced out, a branch without an index would show a sequential or a full index scan."""
    await session.execute(
        text(
            "INSERT INTO system.job (id, kind, status, attempts, max_attempts, finished_at) "
            "SELECT gen_random_uuid(), 'test.finished', 'succeeded', 1, 8, now() FROM generate_series(1, 5000)"
        )
    )
    await session.execute(text("ANALYZE system.job"))
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    # The claim's look as it runs; an `EXPLAIN` of it plans without locking a row.
    candidate = queue._candidate().compile(session.get_bind(), compile_kwargs={"literal_binds": True})

    plan = "\n".join((await session.scalars(text(f"EXPLAIN {candidate}"))).all())

    assert "Seq Scan" not in plan, plan
    # Either partial index on pending jobs serves the first branch; a full index such as the primary key reads all.
    scanned = set(re.findall(r"Index (?:Only )?Scan (?:using|on) (\w+)", plan))
    assert "ix_job_lease" in scanned and scanned <= {"ix_job_claimable", "uq_job_pending_dedupe", "ix_job_lease"}, plan
