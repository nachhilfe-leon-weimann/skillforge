"""The job queue: ``system.job`` in Postgres (calendar-categories spec, decisions B, C, D, E and G; ADR 0011).

Every time is the database's clock (``now()``), so due times and leases compare on one clock whatever the app's
clock says. The functions never commit; a claim is meant to commit at once, so ``running``, the attempt and the
lease are stored before the work starts.

A claim counts the attempt and hands out a ``ClaimedJob``; ``succeed``, ``fail`` and ``defer`` match its id, its
``attempts`` and ``running``, so a holder whose lease passed and whose job was claimed again changes nothing.

The queue relies on READ COMMITTED, Postgres' default: a locked row that another transaction changed after the
statement's snapshot is checked again in its newest version, and ``ON CONFLICT DO UPDATE`` reaches a row newer than
the snapshot. Under a stricter isolation level both end in a serialization error, so never call it in such a session.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Select, and_, case, delete, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import Job, JobStatus

JOB_LEASE = timedelta(minutes=10)
RETRY_BASE = timedelta(minutes=1)
RETRY_CAP = timedelta(hours=1)
DEFAULT_MAX_ATTEMPTS = 8
LEASE_EXPIRED = "LeaseExpired"
"""The ``last_error`` of a job a claim finished: its lease passed on the last attempt, so it never reported back."""

# The predicate of `uq_job_pending_dedupe`: `ON CONFLICT` names it to infer that partial index. A literal, not a
# bound parameter - Postgres infers the index when the query is planned.
_PENDING_ONLY = text("status = 'pending'")
# 2 ** _MAX_DOUBLINGS times RETRY_BASE reaches RETRY_CAP; doubling further only risks an overflow.
_MAX_DOUBLINGS = (RETRY_CAP // RETRY_BASE).bit_length()


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """A job as one claim left it. ``status`` is ``RUNNING`` for a job to work, or ``FAILED`` for one the claim
    finished because its lease passed without an attempt left - nothing to run, only to report."""

    id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    status: JobStatus

    @property
    def runnable(self) -> bool:
        """The caller holds the job and runs it; otherwise the claim finished it, for the caller to report."""
        return self.status is JobStatus.RUNNING


@dataclass(frozen=True, slots=True)
class NewJob:
    """One job for ``enqueue_many``. A kind and a ``dedupe_key`` must determine the payload: a conflict keeps the
    pending job's payload, and a merge (decision C) the twin's."""

    payload: dict[str, Any] = field(default_factory=dict)
    dedupe_key: str | None = None


def retry_delay(attempts: int, retry_after: timedelta | None = None) -> timedelta:
    """Decision E: ``RETRY_BASE`` doubled per attempt made, at most ``RETRY_CAP``; a longer ``retry_after`` wins,
    up to the cap."""
    backoff = RETRY_BASE * 2 ** min(max(attempts - 1, 0), _MAX_DOUBLINGS)
    return min(max(backoff, retry_after or timedelta(0)), RETRY_CAP)


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    dedupe_key: str | None = None,
    delay: timedelta = timedelta(0),
    max_delay: timedelta | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> None:
    """Store one job; see ``enqueue_many``."""
    await enqueue_many(
        session,
        kind,
        [NewJob(payload or {}, dedupe_key)],
        delay=delay,
        max_delay=max_delay,
        max_attempts=max_attempts,
    )


async def enqueue_many(
    session: AsyncSession,
    kind: str,
    jobs: Iterable[NewJob],
    *,
    delay: timedelta = timedelta(0),
    max_delay: timedelta | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> None:
    """Store jobs of ``kind`` due after ``delay``, in one ``INSERT ... ON CONFLICT`` (decision C); nothing for none.

    While a pending job of ``kind`` and a key exists, nothing new is stored for that key. Without ``max_delay`` that
    job stays as it is; with it - a debounced kind, decision D - it becomes due ``delay`` after this call, but never
    later than ``max_delay`` after it was first stored, and never earlier than it already was: a job that waits out a
    backoff keeps it. A running job is no conflict: it may already have read what this call announces.

    A key repeated in ``jobs`` is stored once (its first payload): one statement cannot update a row twice. The rows
    go in in key order, so two calls with overlapping keys wait for each other instead of deadlocking.
    """
    if max_delay is not None and max_delay < delay:
        raise ValueError("max_delay must not be shorter than delay")
    keyed: dict[str, NewJob] = {}
    keyless: list[NewJob] = []
    for job in jobs:
        if job.dedupe_key is None:
            keyless.append(job)
        else:
            keyed.setdefault(job.dedupe_key, job)
    if max_delay is not None and keyless:
        raise ValueError("max_delay needs a dedupe_key: a job without one is never debounced")
    rows = [*(keyed[key] for key in sorted(keyed)), *keyless]
    if not rows:
        return
    statement = insert(Job).values([
        {
            "kind": kind,
            "payload": job.payload,
            "status": JobStatus.PENDING,
            "max_attempts": max_attempts,
            "available_at": func.now() + delay,
            "dedupe_key": job.dedupe_key,
        }
        for job in rows
    ])
    if max_delay is None:
        statement = statement.on_conflict_do_nothing(
            index_elements=[Job.kind, Job.dedupe_key], index_where=_PENDING_ONLY
        )
    else:
        statement = statement.on_conflict_do_update(
            index_elements=[Job.kind, Job.dedupe_key],
            index_where=_PENDING_ONLY,
            set_={
                "available_at": func.greatest(
                    Job.available_at, func.least(Job.created_at + max_delay, statement.excluded.available_at)
                ),
                "updated_at": func.now(),
            },
        )
    await session.execute(statement)


async def claim(session: AsyncSession) -> ClaimedJob | None:
    """Take one due job in one statement (decision B): a ``pending`` job whose ``available_at`` passed, or a
    ``running`` one whose lease passed - its holder is gone or too slow.

    The row is locked with ``SKIP LOCKED``, so concurrent claims never wait for each other and never take the same
    job. The claim makes it ``running``, counts the attempt and sets the lease ``JOB_LEASE``; a job without an
    attempt left is finished as ``failed`` instead and returned so, for the caller to report.
    """
    exhausted = Job.attempts >= Job.max_attempts
    statement = (
        update(Job)
        .where(Job.id == _candidate().scalar_subquery())
        .values(
            status=case(
                (exhausted, literal(JobStatus.FAILED, Job.status.type)),
                else_=literal(JobStatus.RUNNING, Job.status.type),
            ),
            attempts=case((exhausted, Job.attempts), else_=Job.attempts + 1),
            locked_until=case((exhausted, None), else_=func.now() + JOB_LEASE),
            finished_at=case((exhausted, func.now()), else_=None),
            last_error=case((exhausted, LEASE_EXPIRED), else_=Job.last_error),
        )
        .returning(Job.id, Job.kind, Job.payload, Job.attempts, Job.max_attempts, Job.status)
        .execution_options(synchronize_session=False)
    )
    row = (await session.execute(statement)).one_or_none()
    return None if row is None else ClaimedJob(*row)


def _candidate() -> Select[uuid.UUID]:
    """The claim's look for one due job: each branch matches a partial index (``ix_job_claimable``, ``ix_job_lease``),
    so finished jobs are never read."""
    due = or_(
        and_(Job.status == JobStatus.PENDING, Job.available_at <= func.now()),
        and_(Job.status == JobStatus.RUNNING, Job.locked_until <= func.now()),
    )
    return select(Job.id).where(due).order_by(Job.available_at).limit(1).with_for_update(skip_locked=True)


async def succeed(session: AsyncSession, job: ClaimedJob) -> bool:
    """Finish the claimed job as ``succeeded``; ``False`` if the claim is stale and nothing changed."""
    statement = (
        update(Job)
        .where(*_held(job))
        .values(status=JobStatus.SUCCEEDED, locked_until=None, finished_at=func.now())
        .execution_options(synchronize_session=False)
    )
    return cast(CursorResult, await session.execute(statement)).rowcount == 1


async def fail(
    session: AsyncSession, job: ClaimedJob, *, error: str, retry_after: timedelta | None = None
) -> JobStatus | None:
    """Record a failed attempt (decision E): ``PENDING`` when the job is due again after ``retry_delay`` - itself,
    or the pending twin that replaces it (decision C) - ``FAILED`` after its last attempt, ``None`` if the claim is
    stale and nothing changed.

    ``error`` is stored as ``last_error``: an error class and a status, never payload content.
    """
    if job.attempts < job.max_attempts:
        delay = retry_delay(job.attempts, retry_after)
        applied = await _return_to_pending(session, job, attempts=job.attempts, delay=delay, error=error)
        return JobStatus.PENDING if applied else None
    statement = (
        update(Job)
        .where(*_held(job))
        .values(status=JobStatus.FAILED, locked_until=None, finished_at=func.now(), last_error=error)
        .execution_options(synchronize_session=False)
    )
    applied = cast(CursorResult, await session.execute(statement)).rowcount == 1
    return JobStatus.FAILED if applied else None


async def defer(session: AsyncSession, job: ClaimedJob, *, delay: timedelta) -> bool:
    """Make the claimed job due again after ``delay`` without counting the attempt (decision G): the claim's count is
    taken back. A pending twin replaces it as in ``fail``. ``False`` if the claim is stale and nothing changed."""
    return await _return_to_pending(session, job, attempts=job.attempts - 1, delay=delay, error=None)


def _held(job: ClaimedJob) -> tuple[Any, ...]:
    """The claim still holds the job: nobody claimed it since, and nothing finished it."""
    return Job.id == job.id, Job.attempts == job.attempts, Job.status == JobStatus.RUNNING


async def _return_to_pending(
    session: AsyncSession, job: ClaimedJob, *, attempts: int, delay: timedelta, error: str | None
) -> bool:
    """Make a held job pending again, or let a pending twin replace it (decision C).

    An ``UPDATE`` to ``pending`` could meet a twin another transaction is storing and fail on
    ``uq_job_pending_dedupe``. So the job's row is deleted and stored again under its id with ``ON CONFLICT`` on that
    index: Postgres waits for a twin in flight, and then either the job comes back or the job is gone and the twin
    takes the greater ``attempts``, ``max_attempts`` and due time - so a backoff or a deferral holds. A job without a
    ``dedupe_key`` never conflicts.
    """
    removed = (
        await session.execute(
            delete(Job)
            .where(*_held(job))
            .returning(Job.kind, Job.payload, Job.max_attempts, Job.dedupe_key, Job.last_error, Job.created_at)
            .execution_options(synchronize_session=False)
        )
    ).one_or_none()
    if removed is None:
        return False
    statement = insert(Job).values(
        id=job.id,
        kind=removed.kind,
        payload=removed.payload,
        status=JobStatus.PENDING,
        attempts=attempts,
        max_attempts=removed.max_attempts,
        available_at=func.now() + delay,
        dedupe_key=removed.dedupe_key,
        last_error=removed.last_error if error is None else error,
        created_at=removed.created_at,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[Job.kind, Job.dedupe_key],
            index_where=_PENDING_ONLY,
            set_={
                column: func.greatest(getattr(Job, column), statement.excluded[column])
                for column in ("attempts", "max_attempts", "available_at")
            }
            | {"updated_at": func.now()},
        )
    )
    return True
