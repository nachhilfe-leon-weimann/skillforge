"""SkillForge's jobs worker (calendar-categories spec, P0-2; decisions F and G; ADR 0011).

Started as the compose service ``jobs`` (same image as the app, its own entrypoint, the DB connection) via
``python -m app.workers.jobs``. It works one job at a time: ``run_once`` claims a due job, runs the handler of its kind
from ``HANDLERS`` and records the outcome. After a job it looks again at once, so a burst drains without waiting;
idle, it looks every ``POLL_INTERVAL``. A task of its own beats for ``/health`` every ``BEAT_INTERVAL``, independent of
a handler that awaits - it relies on handlers never blocking the event loop.

Every queue call commits in a short transaction of its own, under ``QUEUE_CALL_TIMEOUT``, apart from anything a
handler writes: the claim commits before the handler starts, ``succeed``, ``fail`` and ``defer`` afterwards. A handler
that stores a job of its own kind and key thus stores a twin of the job it works, never a row of the failing job's
transaction.

A line names a job by its id and kind. A handler's error is logged and stored by its class and status only - its
message, its traceback and the payload may carry what the logs exclude.

**Watchdog.** The loop stamps its progress at the start of every cycle, after the claim and after every outcome. When
no stamp came for longer than a handler may run plus ``STALL_MARGIN`` - a handler that ignores cancellation, a call
that hangs past every timeout - the beat task logs ``jobs_loop_stalled`` and ends the process at once with
``os._exit(1)``; compose starts it again, and the job is claimed again after its lease. A stalled loop is no reason to
degrade the beat: ``/health`` would take the API down with it (decision F). And no graceful exit: a handler that ignores
cancellation would hang the shutdown as well.

On SIGTERM or SIGINT the worker takes no new job and cancels the running handler; the job is deferred to be due at
once, its attempt taken back - the stop is no failure of the job, and a handler is safe to repeat. A worker killed
before it could defer leaves the job to its lease.
"""

import asyncio
import os
import re
import signal
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import Database
from app.core.db.models import JobStatus, WorkerCycleStatus
from app.core.logging import configure_logging, get_logger
from app.services.jobs import queue
from app.services.jobs.handler import Handler, JobContext
from app.services.system import WorkerName, record_worker_heartbeat

POLL_INTERVAL = timedelta(seconds=5)
BEAT_INTERVAL = timedelta(seconds=15)
# A few beats, so one slow or missed beat does not flip the worker to unhealthy.
HEARTBEAT_FRESH_FOR = BEAT_INTERVAL * 3
# Below the lease, with room for recording the outcome: a handler never outlives its claim.
HANDLER_TIMEOUT = queue.JOB_LEASE - timedelta(minutes=2)
# A queue call is a few short statements; one that takes longer hangs, and counts as a failing one.
QUEUE_CALL_TIMEOUT = timedelta(seconds=30)
# How much longer than a handler may run the loop may go without progress before the watchdog ends the process.
STALL_MARGIN = timedelta(minutes=1)

# Kind -> the handler that works it. A kind registers its handler here; a job of a kind without one waits (decision G).
HANDLERS: dict[str, Handler] = {}

logger = get_logger(__name__)

_WHOLE_SECONDS = re.compile(r"[0-9]+", re.ASCII)


class HandlerTimeout(Exception):
    """The worker cut a handler off at ``HANDLER_TIMEOUT`` - told apart from a ``TimeoutError`` the handler raises."""


@dataclass(frozen=True, slots=True)
class Cadence:
    """The worker's timings; tests pass shorter ones."""

    poll: timedelta = POLL_INTERVAL
    beat: timedelta = BEAT_INTERVAL
    fresh_for: timedelta = HEARTBEAT_FRESH_FOR
    handler_timeout: timedelta = HANDLER_TIMEOUT
    queue_call_timeout: timedelta = QUEUE_CALL_TIMEOUT
    stall_margin: timedelta = STALL_MARGIN


CADENCE = Cadence()


class _Stopped:
    """The outcome of a handler the stop cancelled."""


_STOPPED = _Stopped()


@dataclass(slots=True)
class WorkerState:
    """What the job loop shares with the beat task: whether the queue is in trouble, when the loop last made progress
    (``time.monotonic()``), and the stop signal."""

    stop: asyncio.Event = field(default_factory=asyncio.Event)
    degraded: bool = False
    progressed_at: float = field(default_factory=time.monotonic)

    def progress(self) -> None:
        self.progressed_at = time.monotonic()


async def run_once(context: JobContext, state: WorkerState, cadence: Cadence = CADENCE) -> bool:
    """Claim one due job, run its handler and record the outcome; ``True`` if a job was claimed, so the caller looks
    again at once.

    Only a failing queue call - the database is in trouble - marks the beat ``DEGRADED``, and the next clean cycle
    clears it. A failing handler never does: it is the job's failure, recorded on the job.
    """
    state.progress()
    try:
        async with _queue_session(context, cadence) as session:
            job = await queue.claim(session)
    except Exception:
        logger.exception("job_claim_failed")
        state.degraded = True
        return False
    state.progress()
    if job is None:
        state.degraded = False
        return False
    try:
        await _work(context, state, job, cadence)
    except Exception:
        logger.exception("job_outcome_failed", job_id=str(job.id), kind=job.kind)
        state.degraded = True
    else:
        state.degraded = False
    state.progress()
    return True


@asynccontextmanager
async def _queue_session(context: JobContext, cadence: Cadence) -> AsyncGenerator[AsyncSession]:
    """One queue call's transaction, bounded by ``queue_call_timeout``: a call that hangs fails as ``TimeoutError``."""
    async with asyncio.timeout(cadence.queue_call_timeout.total_seconds()):
        async with context.database.session() as session:
            yield session


async def _work(context: JobContext, state: WorkerState, job: queue.ClaimedJob, cadence: Cadence) -> None:
    if not job.runnable:
        await report_failed(context, job, queue.LEASE_EXPIRED)
        return
    handler = HANDLERS.get(job.kind)
    if handler is None:
        # Decision G: not an attempt - a newer deploy may know the kind.
        logger.warning("job_kind_unknown", job_id=str(job.id), kind=job.kind)
        async with _queue_session(context, cadence) as session:
            await queue.defer(session, job, delay=queue.RETRY_CAP)
        return
    started = time.perf_counter()
    outcome = await _run_handler(handler, context, job, cadence.handler_timeout, state.stop)
    fields = {
        "job_id": str(job.id),
        "kind": job.kind,
        "attempts": job.attempts,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    if isinstance(outcome, _Stopped):
        async with _queue_session(context, cadence) as session:
            held = await queue.defer(session, job, delay=timedelta(0))
        logger.info("job_interrupted", **fields)
    elif outcome is None:
        async with _queue_session(context, cadence) as session:
            held = await queue.succeed(session, job)
        logger.info("job_succeeded", **fields)
    else:
        error = error_label(outcome)
        async with _queue_session(context, cadence) as session:
            status = await queue.fail(session, job, error=error, retry_after=retry_after(outcome))
        held = status is not None
        logger.warning(
            "job_attempt_failed", **fields, error=error, next_status=None if status is None else status.value
        )
        if status is JobStatus.FAILED:
            await report_failed(context, job, error)
    if not held:
        # Another claim took the job after its lease: the outcome is that claim's to record.
        logger.warning("job_claim_stale", job_id=str(job.id), kind=job.kind, attempts=job.attempts)


async def _run_handler(
    handler: Handler, context: JobContext, job: queue.ClaimedJob, timeout: timedelta, stop: asyncio.Event
) -> BaseException | _Stopped | None:
    """Run the handler under ``timeout`` until it ends or the worker stops: ``None`` when it returned, the exception it
    raised (``HandlerTimeout`` past the timeout, even if it swallowed the cut-off), or ``_STOPPED`` when the worker
    cancelled it for the stop. A ``CancelledError`` the handler raises itself is an exception like any other.

    The exception is returned, not handled here: what the caller raises next carries no context of it.
    """

    async def call() -> None:
        deadline = asyncio.timeout(timeout.total_seconds())
        try:
            async with deadline:
                await handler(context, job.payload)
        except TimeoutError:
            if not deadline.expired():
                raise
        if deadline.expired():
            raise HandlerTimeout

    task = asyncio.create_task(call())
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait((task, stopping), return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopping.cancel()
        stopped = not task.done()
        if stopped:
            task.cancel()
            await asyncio.wait((task,))
    if stopped:
        if not task.cancelled():
            task.exception()  # retrieved, not recorded: the stop decides the outcome
        return _STOPPED
    return asyncio.CancelledError() if task.cancelled() else task.exception()


def error_label(error: BaseException) -> str:
    """The ``last_error`` of a failed attempt: the exception's class, and the HTTP status it carries - never its
    message, which may repeat payload content."""
    status = getattr(error, "status", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return f"{type(error).__name__} {status}"
    return type(error).__name__


def retry_after(error: BaseException) -> timedelta | None:
    """A ``retry_after`` the exception carries in whole seconds - the integration errors keep the ``Retry-After``
    header as sent - capped at ``RETRY_CAP``; ``None`` for anything else, such as an HTTP date."""
    value = getattr(error, "retry_after", None)
    if isinstance(value, str) and _WHOLE_SECONDS.fullmatch(value.strip()):
        cap = int(queue.RETRY_CAP.total_seconds())
        return timedelta(seconds=min(int(value.strip()), cap))
    return None


async def report_failed(context: JobContext, job: queue.ClaimedJob, error: str) -> None:
    """A job became ``failed``: it will not run again (decision I). Logged here; the alert follows with P0-3. Never
    raises: a failing report changes nothing about the job and never degrades the beat."""
    try:
        logger.error("job_failed", job_id=str(job.id), kind=job.kind, attempts=job.attempts, error=error)
    except Exception as exc:
        logger.error("job_report_failed", job_id=str(job.id), kind=job.kind, error=type(exc).__name__)


async def beat(context: JobContext, state: WorkerState, fresh_for: timedelta) -> None:
    """Beat once for ``/health``, in its own transaction; a failing beat is logged, never raised."""
    try:
        async with context.database.session() as session:
            await record_worker_heartbeat(
                session,
                worker_name=WorkerName.JOBS,
                status=WorkerCycleStatus.DEGRADED if state.degraded else WorkerCycleStatus.OK,
                fresh_for=fresh_for,
            )
    except Exception:
        logger.exception("jobs_heartbeat_failed")


async def beat_forever(
    context: JobContext,
    state: WorkerState,
    cadence: Cadence = CADENCE,
    exit_process: Callable[[int], object] = os._exit,
) -> None:
    """Decision F: beat every ``cadence.beat`` until the stop, independent of a handler that awaits; and watch the
    loop - one without progress for longer than a handler may run plus ``cadence.stall_margin`` ends the process."""
    limit = (cadence.handler_timeout + cadence.stall_margin).total_seconds()
    while not state.stop.is_set():
        stalled = time.monotonic() - state.progressed_at
        if stalled > limit:
            logger.error("jobs_loop_stalled", stalled_s=round(stalled, 1))
            exit_process(1)
            state.stop.set()  # only reached when ``exit_process`` returns, as a test's does
            return
        await beat(context, state, cadence.fresh_for)
        await _wait(state.stop, cadence.beat)


async def run_forever(
    context: JobContext,
    state: WorkerState,
    cadence: Cadence = CADENCE,
    exit_process: Callable[[int], object] = os._exit,
) -> None:
    """Work jobs until ``state.stop`` is set: a claimed job is followed by the next look at once, an idle look by
    ``cadence.poll``. The beat runs as a task of its own and ends with the loop; a beat task that ends on its own
    stops the loop, so the process never runs on without its beat."""
    state.progress()
    beating = asyncio.create_task(beat_forever(context, state, cadence, exit_process))
    beating.add_done_callback(lambda task: _beat_ended(task, state))
    logger.info("jobs_started", poll_s=cadence.poll.total_seconds(), beat_s=cadence.beat.total_seconds())
    try:
        while not state.stop.is_set():
            try:
                worked = await run_once(context, state, cadence)
            except Exception:
                # A backstop: run_once handles its failures itself, so one bad cycle never ends the loop.
                logger.exception("jobs_cycle_failed")
                state.degraded, worked = True, False
            if not worked:
                await _wait(state.stop, cadence.poll)
    finally:
        state.stop.set()
        await asyncio.wait((beating,))
        if not beating.cancelled():
            beating.exception()  # retrieved: ``_beat_ended`` logged it
        logger.info("jobs_stopped")


def _beat_ended(task: asyncio.Task[None], state: WorkerState) -> None:
    """A beat task that raised, was cancelled or returned before the stop: log it and stop the loop."""
    if task.cancelled():
        error: str | None = "CancelledError"
    elif (exc := task.exception()) is not None:
        error = type(exc).__name__
    elif state.stop.is_set():
        return
    else:
        error = None
    logger.error("jobs_beat_stopped", error=error)
    state.stop.set()


async def _wait(stop: asyncio.Event, interval: timedelta) -> None:
    """Wait ``interval``, or less once the stop is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=interval.total_seconds())
    except TimeoutError:
        pass


async def _serve() -> None:
    settings = get_settings()
    database = Database.from_url(str(settings.db.url))
    state = WorkerState()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, state.stop.set)
        except NotImplementedError:  # pragma: no cover - signal handlers unavailable (e.g. Windows)
            pass
    try:
        await run_forever(JobContext(database=database, settings=settings), state)
    finally:
        await database.dispose()


def main() -> None:
    configure_logging(get_settings().logging)
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
