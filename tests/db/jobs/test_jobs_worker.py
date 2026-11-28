"""The jobs worker against the real queue and ``/health`` (calendar-categories spec, P0-2; decisions E, F and G).

These tests commit - the worker opens its own transactions - so the fixture clears ``system.job`` and the heartbeats
before and after. Every time is asserted against the database's clock, read before and after the cycle.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update

from app.core.config import Settings
from app.core.db import Database
from app.core.db.dependencies import get_database
from app.core.db.models import Job, JobStatus, WorkerCycleStatus, WorkerHeartbeat
from app.core.logging import LogFormat, LoggingSettings, LogLevel, configure_logging
from app.main import app
from app.services.jobs import queue
from app.services.jobs.handler import JobContext
from app.services.system import WorkerName, record_worker_heartbeat
from app.workers import jobs as worker

pytestmark = pytest.mark.db

KIND = "test.worker"
SECRET = "synthetic-secret-7f3a"
PAYLOAD_SECRET = "synthetic-payload-c91e"
# Short enough that a test waiting on the worker ends quickly when it fails.
WAIT = 5
FAST = worker.Cadence(
    poll=timedelta(milliseconds=50), beat=timedelta(milliseconds=50), fresh_for=timedelta(milliseconds=300)
)


@pytest.fixture
async def context(db: Database) -> AsyncIterator[JobContext]:
    async def clear() -> None:
        async with db.session() as cleanup:
            await cleanup.execute(delete(Job))
            await cleanup.execute(delete(WorkerHeartbeat))

    await clear()
    try:
        yield JobContext(database=db, settings=Settings(_env_file=None))
    finally:
        await clear()


async def _enqueue(db: Database, kind: str = KIND, payload: dict[str, Any] | None = None, **options: Any) -> None:
    async with db.session() as session:
        await queue.enqueue(session, kind, payload, **options)


async def _one(db: Database) -> Job:
    async with db.session(write=False) as session:
        (job,) = await session.scalars(select(Job))
        return job


async def _now(db: Database) -> datetime:
    async with db.session(write=False) as session:
        return (await session.execute(select(func.now()))).scalar_one()


async def _health(db: Database) -> tuple[int, dict[str, str]]:
    """``GET /health`` with a fresh housekeeping beat, so the jobs worker alone decides the workers' part."""
    async with db.session() as session:
        await record_worker_heartbeat(
            session, worker_name=WorkerName.HOUSEKEEPING, status=WorkerCycleStatus.OK, fresh_for=timedelta(minutes=5)
        )
    app.dependency_overrides[get_database] = lambda: db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.get("/health")
    finally:
        app.dependency_overrides.clear()
    return response.status_code, response.json()["workers"]["checks"]


async def _until(db: Database, status: JobStatus) -> Job:
    async def poll() -> Job:
        while (job := await _one(db)).status is not status:
            await asyncio.sleep(0.02)
        return job

    return await asyncio.wait_for(poll(), timeout=WAIT)


class _Throttled(Exception):
    status = 429
    retry_after = "600"


@pytest.mark.parametrize(
    ("error", "label", "delay"),
    [
        (RuntimeError("synthetic"), "RuntimeError", queue.RETRY_BASE),
        (_Throttled(), "_Throttled 429", timedelta(minutes=10)),
    ],
)
async def test_a_failing_handler_leaves_its_job_pending_with_the_next_due_time_and_health_ok(
    context: JobContext, monkeypatch, error: Exception, label: str, delay: timedelta
):
    async def raises(context: JobContext, payload: Mapping[str, Any]) -> None:
        raise error

    monkeypatch.setitem(worker.HANDLERS, KIND, raises)
    await _enqueue(context.database)
    state = worker.WorkerState()

    before = await _now(context.database)
    assert await worker.run_once(context, state) is True
    after = await _now(context.database)

    job = await _one(context.database)
    assert (job.status, job.attempts, job.last_error) == (JobStatus.PENDING, 1, label)
    assert before + delay <= job.available_at <= after + delay
    await worker.beat(context, state, timedelta(minutes=1))
    assert await _health(context.database) == (200, {"housekeeping": "ok", "jobs": "ok"})


async def test_a_handler_that_runs_longer_than_the_beat_stays_fresh_leaves_health_ok(context: JobContext, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(context: JobContext, payload: Mapping[str, Any]) -> None:
        started.set()
        await release.wait()

    monkeypatch.setitem(worker.HANDLERS, KIND, slow)
    await _enqueue(context.database)
    state = worker.WorkerState()
    running = asyncio.create_task(worker.run_forever(context, state, FAST))
    try:
        await asyncio.wait_for(started.wait(), timeout=WAIT)
        # Well past the beat's freshness window while the handler still runs.
        await asyncio.sleep(FAST.fresh_for.total_seconds() * 3)
        assert await _health(context.database) == (200, {"housekeeping": "ok", "jobs": "ok"})
        release.set()
        assert (await _until(context.database, JobStatus.SUCCEEDED)).attempts == 1
    finally:
        state.stop.set()
        await asyncio.wait_for(running, timeout=WAIT)


async def test_a_job_of_an_unknown_kind_keeps_its_attempts_and_is_due_again_after_the_cap(context: JobContext):
    await _enqueue(context.database, "test.nobody-handles-this")

    before = await _now(context.database)
    assert await worker.run_once(context, worker.WorkerState()) is True
    after = await _now(context.database)

    job = await _one(context.database)
    assert (job.status, job.attempts, job.last_error) == (JobStatus.PENDING, 0, None)
    assert before + queue.RETRY_CAP <= job.available_at <= after + queue.RETRY_CAP


async def test_a_job_stored_while_the_worker_is_stopped_is_worked_after_its_start(context: JobContext, monkeypatch):
    seen: list[Mapping[str, Any]] = []

    async def records(context: JobContext, payload: Mapping[str, Any]) -> None:
        seen.append(dict(payload))

    monkeypatch.setitem(worker.HANDLERS, KIND, records)
    await _enqueue(context.database, payload={"n": 1})

    state = worker.WorkerState()
    running = asyncio.create_task(worker.run_forever(context, state, FAST))
    try:
        job = await _until(context.database, JobStatus.SUCCEEDED)
    finally:
        state.stop.set()
        await asyncio.wait_for(running, timeout=WAIT)

    assert (job.attempts, seen) == (1, [{"n": 1}])


async def test_a_stop_cancels_the_running_handler_and_leaves_the_job_due_at_once(context: JobContext, monkeypatch):
    started = asyncio.Event()

    async def hangs(context: JobContext, payload: Mapping[str, Any]) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setitem(worker.HANDLERS, KIND, hangs)
    await _enqueue(context.database)
    state = worker.WorkerState()
    running = asyncio.create_task(worker.run_forever(context, state, FAST))
    await asyncio.wait_for(started.wait(), timeout=WAIT)

    state.stop.set()
    await asyncio.wait_for(running, timeout=WAIT)

    job = await _one(context.database)
    assert (job.status, job.attempts, job.locked_until) == (JobStatus.PENDING, 0, None)
    assert job.available_at <= await _now(context.database)


async def test_health_is_unhealthy_without_a_fresh_ok_beat_of_jobs(context: JobContext):
    assert await _health(context.database) == (503, {"housekeeping": "ok", "jobs": "unhealthy"})

    for status, fresh_for in ((WorkerCycleStatus.OK, timedelta(0)), (WorkerCycleStatus.DEGRADED, timedelta(minutes=1))):
        async with context.database.session() as session:
            await record_worker_heartbeat(session, worker_name=WorkerName.JOBS, status=status, fresh_for=fresh_for)
        assert await _health(context.database) == (503, {"housekeeping": "ok", "jobs": "unhealthy"})


async def test_a_job_the_claim_finished_is_reported_as_failed(context: JobContext, restore_logging, capsys):
    await _enqueue(context.database, max_attempts=1)
    async with context.database.session() as session:
        claimed = await queue.claim(session)
        assert claimed is not None
        await session.execute(update(Job).values(locked_until=func.now()))
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()

    assert await worker.run_once(context, worker.WorkerState()) is True

    failed = [line for line in _lines(capsys.readouterr().out) if line["event"] == "job_failed"]
    assert [(line["job_id"], line["kind"], line["attempts"], line["error"]) for line in failed] == [
        (str(claimed.id), KIND, 1, queue.LEASE_EXPIRED)
    ]


async def test_a_handler_error_leaves_its_message_and_the_payload_out_of_the_log_and_last_error(
    context: JobContext, monkeypatch, restore_logging, capsys
):
    async def leaks(context: JobContext, payload: Mapping[str, Any]) -> None:
        raise ValueError(f"cannot handle {payload['title']}: {SECRET}")

    monkeypatch.setitem(worker.HANDLERS, KIND, leaks)
    # One attempt only, so the same failure also takes the path of the failure report.
    await _enqueue(context.database, payload={"title": PAYLOAD_SECRET}, max_attempts=1)
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    capsys.readouterr()

    await worker.run_once(context, worker.WorkerState())

    captured = capsys.readouterr()
    output = captured.out + captured.err
    events = [line["event"] for line in _lines(captured.out)]
    assert "job_attempt_failed" in events and "job_failed" in events
    job = await _one(context.database)
    assert (job.status, job.last_error) == (JobStatus.FAILED, "ValueError")
    for secret in (SECRET, PAYLOAD_SECRET):
        assert secret not in output
        assert secret not in (job.last_error or "")


@pytest.fixture
def restore_logging():
    """Put the logging configuration back, so the turned-up level ends with the test."""
    yield
    configure_logging(LoggingSettings())


def _lines(output: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]
