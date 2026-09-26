from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from app.core.db import Database
from app.core.db.models import WorkerCycleStatus, WorkerHeartbeat
from app.services.system import (
    HealthStatus,
    WorkerName,
    check_worker_health,
    read_worker_heartbeat,
    record_worker_heartbeat,
)

HOUSEKEEPING = WorkerName.HOUSEKEEPING
FRESH_FOR = timedelta(seconds=90)


@pytest.mark.db
async def test_record_then_read_returns_current_beat(session):
    await record_worker_heartbeat(
        session, worker_name=HOUSEKEEPING, status=WorkerCycleStatus.OK, fresh_for=FRESH_FOR, detail={"jobs": 3}
    )

    heartbeat = await read_worker_heartbeat(session, HOUSEKEEPING)

    assert heartbeat is not None
    assert heartbeat.last_status is WorkerCycleStatus.OK
    assert heartbeat.detail == {"jobs": 3}
    assert heartbeat.expires_at > heartbeat.last_beat_at


@pytest.mark.db
async def test_record_upserts_in_place(session):
    await record_worker_heartbeat(session, worker_name=HOUSEKEEPING, status=WorkerCycleStatus.OK, fresh_for=FRESH_FOR)
    await record_worker_heartbeat(
        session, worker_name=HOUSEKEEPING, status=WorkerCycleStatus.DEGRADED, fresh_for=FRESH_FOR
    )

    heartbeat = await read_worker_heartbeat(session, HOUSEKEEPING)

    assert heartbeat is not None
    assert heartbeat.last_status is WorkerCycleStatus.DEGRADED


@pytest.mark.db
async def test_check_worker_health_healthy_for_fresh_ok_beat(session):
    await record_worker_heartbeat(session, worker_name=HOUSEKEEPING, status=WorkerCycleStatus.OK, fresh_for=FRESH_FOR)

    result = await check_worker_health(WorkerName.HOUSEKEEPING, _database(session))

    assert result.status is HealthStatus.HEALTHY


@pytest.mark.db
async def test_check_worker_health_unhealthy_for_degraded_beat(session):
    await record_worker_heartbeat(
        session, worker_name=HOUSEKEEPING, status=WorkerCycleStatus.DEGRADED, fresh_for=FRESH_FOR
    )

    result = await check_worker_health(WorkerName.HOUSEKEEPING, _database(session))

    assert result.status is HealthStatus.UNHEALTHY


@pytest.mark.db
async def test_check_worker_health_unhealthy_without_beat(session):
    result = await check_worker_health(WorkerName.HOUSEKEEPING, _database(session))

    assert result.status is HealthStatus.UNHEALTHY


@pytest.mark.db
async def test_check_worker_health_unhealthy_for_stale_beat(session):
    # A beat whose freshness window already lapsed (expires_at in the past).
    past = datetime(2020, 1, 1, tzinfo=UTC)
    session.add(
        WorkerHeartbeat(
            worker_name=HOUSEKEEPING,
            last_beat_at=past,
            expires_at=past + FRESH_FOR,
            last_status=WorkerCycleStatus.OK,
        )
    )
    await session.flush()

    result = await check_worker_health(WorkerName.HOUSEKEEPING, _database(session))

    assert result.status is HealthStatus.UNHEALTHY


def _database(session) -> Database:
    """Wrap the test's transactional session as a Database so the health check reads
    within the same (uncommitted) transaction -- keeping the rollback isolation intact."""

    class _OneSessionDatabase:
        @asynccontextmanager
        async def session(self, *, write: bool = True):
            yield session

    return cast(Database, _OneSessionDatabase())
