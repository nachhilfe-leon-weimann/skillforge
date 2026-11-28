"""The jobs worker's cycle without a database (calendar-categories spec, P0-2; decision F): what degrades the beat,
what a failed attempt is recorded as, what a stop does, and the watchdog. The queue and the heartbeat are stubbed;
``tests/db/jobs/`` runs them for real.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import cast
from unittest.mock import ANY, AsyncMock

import pytest

from app.core.config import Settings
from app.core.db import Database
from app.core.db.models import JobStatus
from app.services.jobs import queue
from app.services.jobs.handler import JobContext
from app.workers import jobs as worker

KIND = "test.kind"
# Timings for the tests that run the loop: a handler may run 100 ms, the loop may go 150 ms without progress.
FAST = worker.Cadence(
    poll=timedelta(milliseconds=10),
    beat=timedelta(milliseconds=10),
    handler_timeout=timedelta(milliseconds=100),
    queue_call_timeout=timedelta(milliseconds=50),
    stall_margin=timedelta(milliseconds=50),
)


class _StubDatabase:
    def __init__(self) -> None:
        self.sessions = 0

    @asynccontextmanager
    async def session(self, *, write: bool = True):
        self.sessions += 1
        yield object()


class _Throttled(Exception):
    def __init__(self, status: object, retry_after: object = None) -> None:
        super().__init__("synthetic")
        self.status = status
        self.retry_after = retry_after


def _job(*, status: JobStatus = JobStatus.RUNNING) -> queue.ClaimedJob:
    return queue.ClaimedJob(id=uuid.uuid4(), kind=KIND, payload={}, attempts=1, max_attempts=8, status=status)


@pytest.fixture
def context() -> JobContext:
    return JobContext(database=cast(Database, _StubDatabase()), settings=cast(Settings, object()))


@pytest.fixture
def stub_queue(monkeypatch) -> dict[str, AsyncMock]:
    mocks = {
        "claim": AsyncMock(return_value=_job()),
        "succeed": AsyncMock(return_value=True),
        "fail": AsyncMock(return_value=JobStatus.PENDING),
        "defer": AsyncMock(return_value=True),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(queue, name, mock)
    return mocks


@pytest.fixture(autouse=True)
def heartbeat(monkeypatch) -> AsyncMock:
    """Stub the heartbeat write - the stub database has no real session to upsert into."""
    mock = AsyncMock()
    monkeypatch.setattr(worker, "record_worker_heartbeat", mock)
    return mock


class _StubLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def _record(self, event: str, **fields) -> None:
        self.events.append((event, fields))

    info = warning = error = exception = _record

    def named(self, event: str) -> list[dict]:
        return [fields for name, fields in self.events if name == event]


@pytest.fixture
def log(monkeypatch) -> _StubLogger:
    stub = _StubLogger()
    monkeypatch.setattr(worker, "logger", stub)
    return stub


def _handle(monkeypatch, handler) -> None:
    monkeypatch.setitem(worker.HANDLERS, KIND, handler)


async def _swallows_cancellation(release: asyncio.Event) -> None:
    """Wait for ``release``, ignoring every cancellation on the way - a handler that breaks the rules."""
    while not release.is_set():
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass


class _Exits:
    """A stand-in for ``os._exit`` that records the code and returns."""

    def __init__(self) -> None:
        self.codes: list[int] = []
        self.called = asyncio.Event()

    def __call__(self, code: int) -> None:
        self.codes.append(code)
        self.called.set()


def test_the_handler_timeout_lies_below_the_lease_and_the_beat_window_above_the_beat():
    assert worker.HANDLER_TIMEOUT + worker.QUEUE_CALL_TIMEOUT < queue.JOB_LEASE
    assert worker.QUEUE_CALL_TIMEOUT < worker.STALL_MARGIN
    assert worker.HEARTBEAT_FRESH_FOR > worker.BEAT_INTERVAL
    assert worker.POLL_INTERVAL == timedelta(seconds=5)


@pytest.mark.parametrize(
    ("error", "label"),
    [
        (RuntimeError("synthetic"), "RuntimeError"),
        (_Throttled(429), "_Throttled 429"),
        (_Throttled(True), "_Throttled"),
        (_Throttled("503"), "_Throttled"),
        (_Throttled(None), "_Throttled"),
    ],
)
def test_the_error_label_is_the_class_and_an_integer_status(error: Exception, label: str):
    assert worker.error_label(error) == label


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("120", timedelta(seconds=120)),
        (" 30 ", timedelta(seconds=30)),
        ("0", timedelta(0)),
        ("9" * 40, queue.RETRY_CAP),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        ("1.5", None),
        ("-5", None),
        ("١٢", None),
        (120, None),
        (None, None),
    ],
)
def test_retry_after_is_read_in_whole_seconds_only(value: object, expected: timedelta | None):
    assert worker.retry_after(_Throttled(503, value)) == expected


async def test_a_failing_claim_degrades_the_beat_and_the_next_clean_cycle_clears_it(context, stub_queue):
    state = worker.WorkerState()
    stub_queue["claim"].side_effect = RuntimeError("db down")

    assert await worker.run_once(context, state) is False
    assert state.degraded is True

    stub_queue["claim"].side_effect = None
    stub_queue["claim"].return_value = None
    assert await worker.run_once(context, state) is False
    assert state.degraded is False


async def test_a_failing_handler_never_degrades_the_beat(context, stub_queue, monkeypatch):
    state = worker.WorkerState(degraded=True)
    _handle(monkeypatch, AsyncMock(side_effect=_Throttled(503, "90")))

    assert await worker.run_once(context, state) is True

    assert state.degraded is False
    assert stub_queue["fail"].await_args.kwargs == {"error": "_Throttled 503", "retry_after": timedelta(seconds=90)}


@pytest.mark.parametrize("call", ["succeed", "fail"])
async def test_a_failing_outcome_degrades_the_beat(context, stub_queue, monkeypatch, call: str):
    state = worker.WorkerState()
    _handle(monkeypatch, AsyncMock(side_effect=None if call == "succeed" else RuntimeError("synthetic")))
    stub_queue[call].side_effect = RuntimeError("db down")

    assert await worker.run_once(context, state) is True
    assert state.degraded is True


async def test_a_job_that_becomes_failed_is_reported_once(context, stub_queue, monkeypatch):
    report = AsyncMock()
    monkeypatch.setattr(worker, "report_failed", report)
    _handle(monkeypatch, AsyncMock(side_effect=_Throttled(500)))
    stub_queue["fail"].return_value = JobStatus.FAILED

    await worker.run_once(context, worker.WorkerState())

    report.assert_awaited_once_with(context, stub_queue["claim"].return_value, "_Throttled 500")


async def test_a_job_the_claim_finished_is_reported_and_not_run(context, stub_queue, monkeypatch):
    report = AsyncMock()
    handler = AsyncMock()
    monkeypatch.setattr(worker, "report_failed", report)
    _handle(monkeypatch, handler)
    finished = _job(status=JobStatus.FAILED)
    stub_queue["claim"].return_value = finished

    assert await worker.run_once(context, worker.WorkerState()) is True

    report.assert_awaited_once_with(context, finished, queue.LEASE_EXPIRED)
    handler.assert_not_awaited()
    for call in ("succeed", "fail", "defer"):
        stub_queue[call].assert_not_awaited()


async def test_a_handler_past_its_timeout_is_a_failed_attempt(context, stub_queue, monkeypatch):
    async def hangs(context, payload):
        await asyncio.Event().wait()

    _handle(monkeypatch, hangs)
    cadence = worker.Cadence(handler_timeout=timedelta(milliseconds=20))

    await asyncio.wait_for(worker.run_once(context, worker.WorkerState(), cadence), timeout=5)

    assert stub_queue["fail"].await_args.kwargs["error"] == "HandlerTimeout"


async def test_a_handler_that_swallows_the_cut_off_and_returns_still_failed(context, stub_queue, monkeypatch):
    async def swallows(context, payload):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    _handle(monkeypatch, swallows)
    cadence = worker.Cadence(handler_timeout=timedelta(milliseconds=20))

    await asyncio.wait_for(worker.run_once(context, worker.WorkerState(), cadence), timeout=5)

    assert stub_queue["fail"].await_args.kwargs["error"] == "HandlerTimeout"
    stub_queue["succeed"].assert_not_awaited()


@pytest.mark.parametrize(
    ("raised", "label"), [(TimeoutError(), "TimeoutError"), (asyncio.CancelledError(), "CancelledError")]
)
async def test_a_timeout_or_cancellation_the_handler_raises_itself_is_a_failed_attempt(
    context, stub_queue, monkeypatch, raised: BaseException, label: str
):
    _handle(monkeypatch, AsyncMock(side_effect=raised))
    state = worker.WorkerState()

    assert await asyncio.wait_for(worker.run_once(context, state), timeout=5) is True

    assert stub_queue["fail"].await_args.kwargs["error"] == label
    stub_queue["defer"].assert_not_awaited()
    assert state.degraded is False


async def test_a_stop_cancels_the_handler_and_defers_the_job_to_be_due_at_once(context, stub_queue, monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def hangs(context, payload):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    _handle(monkeypatch, hangs)
    state = worker.WorkerState()
    cycle = asyncio.create_task(worker.run_once(context, state))
    await asyncio.wait_for(started.wait(), timeout=5)
    state.stop.set()

    assert await asyncio.wait_for(cycle, timeout=5) is True
    assert cancelled.is_set()
    stub_queue["defer"].assert_awaited_once_with(ANY, stub_queue["claim"].return_value, delay=timedelta(0))
    stub_queue["fail"].assert_not_awaited()
    stub_queue["succeed"].assert_not_awaited()


async def test_a_job_of_an_unknown_kind_is_deferred_by_the_cap(context, stub_queue):
    stub_queue["claim"].return_value = queue.ClaimedJob(
        id=uuid.uuid4(),
        kind="test.nobody-handles-this",
        payload={},
        attempts=1,
        max_attempts=8,
        status=JobStatus.RUNNING,
    )

    assert await worker.run_once(context, worker.WorkerState()) is True

    assert stub_queue["defer"].await_args.kwargs == {"delay": queue.RETRY_CAP}
    stub_queue["fail"].assert_not_awaited()


async def test_a_failing_report_is_logged_by_its_class_and_never_degrades_the_beat(
    context, stub_queue, monkeypatch, log
):
    def refuses(event: str, **fields) -> None:
        if event == "job_failed":
            raise RuntimeError("synthetic alert failure")
        log._record(event, **fields)

    monkeypatch.setattr(log, "error", refuses)
    _handle(monkeypatch, AsyncMock(side_effect=_Throttled(500)))
    stub_queue["fail"].return_value = JobStatus.FAILED
    state = worker.WorkerState()

    assert await worker.run_once(context, state) is True

    assert state.degraded is False
    assert [fields["error"] for fields in log.named("job_report_failed")] == ["RuntimeError"]
    assert log.named("job_outcome_failed") == []


async def test_a_queue_call_that_hangs_is_a_failing_one_and_the_loop_goes_on(context, stub_queue, log):
    calls = 0

    async def hangs_once(session):
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.Event().wait()
        return None

    stub_queue["claim"].side_effect = hangs_once
    state, exits = worker.WorkerState(), _Exits()
    running = asyncio.create_task(worker.run_forever(context, state, FAST, exits))

    async def looked_again() -> None:
        while calls < 3:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(looked_again(), timeout=5)
    state.stop.set()
    await asyncio.wait_for(running, timeout=5)

    assert len(log.named("job_claim_failed")) == 1
    assert state.degraded is False, "the clean cycles after the hang cleared it"
    assert exits.codes == []


async def test_a_handler_that_ignores_cancellation_past_its_timeout_trips_the_watchdog(
    context, stub_queue, monkeypatch, log
):
    release = asyncio.Event()
    _handle(monkeypatch, lambda context, payload: _swallows_cancellation(release))
    state, exits = worker.WorkerState(), _Exits()
    running = asyncio.create_task(worker.run_forever(context, state, FAST, exits))
    try:
        await asyncio.wait_for(exits.called.wait(), timeout=5)
    finally:
        release.set()
        state.stop.set()
        await asyncio.wait_for(running, timeout=5)

    assert exits.codes == [1]
    (stalled,) = log.named("jobs_loop_stalled")
    assert set(stalled) == {"stalled_s"}
    assert stalled["stalled_s"] > (FAST.handler_timeout + FAST.stall_margin).total_seconds()


@pytest.mark.parametrize("busy", [False, True], ids=["idle", "long-handlers-in-time"])
async def test_a_loop_that_makes_progress_never_trips_the_watchdog(context, stub_queue, monkeypatch, busy: bool):
    if busy:
        # Each job takes 90 % of its timeout - far longer than the stall margin alone.
        _handle(monkeypatch, AsyncMock(side_effect=lambda context, payload: asyncio.sleep(0.09)))
    else:
        stub_queue["claim"].return_value = None
    state, exits = worker.WorkerState(), _Exits()
    running = asyncio.create_task(worker.run_forever(context, state, FAST, exits))

    # Several times the watchdog's limit.
    await asyncio.sleep((FAST.handler_timeout + FAST.stall_margin).total_seconds() * 5)
    state.stop.set()
    await asyncio.wait_for(running, timeout=5)

    assert exits.codes == []
    if busy:
        assert stub_queue["succeed"].await_count >= 5
        stub_queue["fail"].assert_not_awaited()


async def test_a_beat_task_that_dies_stops_the_worker(context, stub_queue, monkeypatch, log):
    stub_queue["claim"].return_value = None
    monkeypatch.setattr(worker, "beat", AsyncMock(side_effect=RuntimeError("synthetic")))
    state = worker.WorkerState()

    await asyncio.wait_for(worker.run_forever(context, state, FAST, _Exits()), timeout=5)

    assert state.stop.is_set()
    assert log.named("jobs_beat_stopped") == [{"error": "RuntimeError"}]
    assert log.named("jobs_stopped") == [{}]
