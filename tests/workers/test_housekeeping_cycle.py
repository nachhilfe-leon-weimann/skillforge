from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import ANY, AsyncMock

import pytest

from app.core.db import Database
from app.core.db.models import WorkerCycleStatus
from app.services.system import WorkerName
from app.workers import housekeeping as worker

COUNTERS = {"sessions_deleted", "action_tokens_deleted"}


@pytest.fixture(autouse=True)
def heartbeat(monkeypatch) -> AsyncMock:
    """Stub the heartbeat write - the stub database has no real session to upsert into."""
    mock = AsyncMock()
    monkeypatch.setattr(worker, "record_worker_heartbeat", mock)
    return mock


@pytest.fixture
def passes(monkeypatch) -> dict[str, AsyncMock]:
    mocks = {"sessions_deleted": AsyncMock(return_value=3), "action_tokens_deleted": AsyncMock(return_value=1)}
    for counter, mock in mocks.items():
        monkeypatch.setitem(worker.PASSES, counter, mock)
    return mocks


class _StubLogger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.exceptions: list[tuple[str, dict]] = []

    def info(self, event, **fields):
        self.calls.append((event, fields))

    def exception(self, event, **fields):
        self.exceptions.append((event, fields))


class _StubDatabase:
    def __init__(self) -> None:
        # One fresh object per `session()` entry, so a test can tell whether two passes (or a pass and the
        # heartbeat) shared a transaction instead of each getting its own.
        self.sessions: list[object] = []

    @asynccontextmanager
    async def session(self, *, write: bool = True):
        session = object()
        self.sessions.append(session)
        yield session


async def _cycle() -> tuple[_StubLogger, _StubDatabase]:
    logger = _StubLogger()
    database = _StubDatabase()
    await worker.run_cycle(cast(Database, database), logger)
    return logger, database


async def test_a_cycle_logs_exactly_one_line_with_the_counts(passes):
    logger, _ = await _cycle()

    assert [event for event, _ in logger.calls] == ["housekeeping_cycle"]
    fields = logger.calls[0][1]
    assert set(fields) == COUNTERS | {"duration_ms"}
    assert (fields["sessions_deleted"], fields["action_tokens_deleted"]) == (3, 1)


async def test_each_pass_runs_one_batch_per_cycle(passes, heartbeat):
    _, database = await _cycle()

    # Two passes, one heartbeat - each in its own transaction, so a failing one never rolls back another.
    assert len(database.sessions) == 3
    assert len(set(database.sessions)) == 3
    for mock, session in zip(passes.values(), database.sessions, strict=False):
        mock.assert_awaited_once_with(session, limit=worker.DELETE_BATCH_LIMIT)
    assert heartbeat.await_args.args[0] is database.sessions[2]


@pytest.mark.parametrize("failing", sorted(COUNTERS))
async def test_a_failing_pass_degrades_the_beat_and_leaves_the_other(passes, heartbeat, failing: str):
    passes[failing].side_effect = RuntimeError("boom")

    logger, _ = await _cycle()

    assert [event for event, _ in logger.calls] == ["housekeeping_cycle"]
    assert logger.exceptions == [("housekeeping_pass_failed", {"counter": failing})]
    other = next(counter for counter in COUNTERS if counter != failing)
    assert logger.calls[0][1] == {failing: 0, other: passes[other].return_value, "duration_ms": ANY}
    assert heartbeat.await_args.kwargs["status"] is WorkerCycleStatus.DEGRADED


async def test_a_clean_cycle_beats_ok_under_housekeeping(passes, heartbeat):
    await _cycle()

    kwargs = heartbeat.await_args.kwargs
    assert kwargs["worker_name"] is WorkerName.HOUSEKEEPING
    assert kwargs["status"] is WorkerCycleStatus.OK
    assert kwargs["fresh_for"] == worker.HEARTBEAT_FRESH_FOR
    assert set(kwargs["detail"]) == COUNTERS | {"duration_ms"}


async def test_a_failing_heartbeat_is_logged_and_never_raised(passes, heartbeat):
    heartbeat.side_effect = RuntimeError("db down")

    logger, _ = await _cycle()

    assert ("housekeeping_heartbeat_failed", {}) in logger.exceptions
