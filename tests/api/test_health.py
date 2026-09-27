from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient

from app.core.db.dependencies import get_database
from app.core.db.models import WorkerCycleStatus, WorkerHeartbeat
from app.main import app
from app.services.system import HealthStatus, WorkerHealthCheckResponse
from app.services.system.health_service import _aggregate_health_status, _worker_status_from_heartbeat

# --- liveness ---------------------------------------------------------------


async def test_liveness_is_always_ok():
    async with _client(_FakeDatabase()) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- dependencies -----------------------------------------------------------


async def test_dependency_database_healthy():
    database = _FakeDatabase(healthy=True)

    async with _client(database) as client:
        response = await client.get("/health/dependencies/database")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "dependency_name": "database"}
    # Health shares the app-lifespan engine and must never dispose it (#57).
    assert database.disposed is False


async def test_dependency_database_unhealthy():
    async with _client(_FakeDatabase(healthy=False)) as client:
        response = await client.get("/health/dependencies/database")

    assert response.status_code == 503
    assert response.json() == {"status": "unhealthy", "dependency_name": "database"}


async def test_dependency_check_error_maps_to_500():
    async with _client(_FakeDatabase(error=True)) as client:
        response = await client.get("/health/dependencies/database")

    assert response.status_code == 500
    assert response.json() == {"status": "error", "dependency_name": "database"}


async def test_dependencies_aggregate_healthy():
    async with _client(_FakeDatabase(healthy=True)) as client:
        response = await client.get("/health/dependencies")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok"}}


async def test_dependencies_aggregate_unhealthy():
    async with _client(_FakeDatabase(healthy=False)) as client:
        response = await client.get("/health/dependencies")

    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "unhealthy"


# --- workers ----------------------------------------------------------------


async def test_worker_endpoint_reports_known_worker(monkeypatch):
    # The check itself reads the heartbeat from the DB (covered by DB tests); here we only
    # assert the endpoint wiring + status mapping, so stub the check out.
    async def _healthy(worker_name, database):
        return WorkerHealthCheckResponse(worker_name=worker_name, status=HealthStatus.HEALTHY)

    monkeypatch.setattr("app.api.system.health.check_worker_health", _healthy)

    async with _client(_FakeDatabase()) as client:
        response = await client.get("/health/workers/housekeeping")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "worker_name": "housekeeping"}


# --- system -----------------------------------------------------------------


async def test_system_health_ok_when_all_checks_pass(monkeypatch):
    _force_workers_healthy(monkeypatch)

    async with _client(_FakeDatabase(healthy=True)) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == app.version
    assert body["dependencies"]["checks"]["database"] == "ok"
    assert body["workers"]["checks"] == {"housekeeping": "ok"}


async def test_system_health_503_when_dependency_down(monkeypatch):
    _force_workers_healthy(monkeypatch)

    async with _client(_FakeDatabase(healthy=False)) as client:
        response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"


async def test_system_health_reports_the_version_while_degraded(monkeypatch):
    _force_workers_healthy(monkeypatch)

    async with _client(_FakeDatabase(healthy=False)) as client:
        response = await client.get("/health")

    # The deploy job compares this against the released version; it must not depend on the status.
    assert response.status_code == 503
    assert response.json()["version"] == app.version


# --- service units ----------------------------------------------------------


def test_aggregate_health_status_picks_worst():
    assert _aggregate_health_status() is HealthStatus.HEALTHY
    assert _aggregate_health_status(HealthStatus.HEALTHY, HealthStatus.HEALTHY) is HealthStatus.HEALTHY
    assert _aggregate_health_status(HealthStatus.HEALTHY, HealthStatus.UNHEALTHY) is HealthStatus.UNHEALTHY
    assert _aggregate_health_status(HealthStatus.UNHEALTHY, HealthStatus.ERROR) is HealthStatus.ERROR


def test_worker_status_unhealthy_without_heartbeat():
    now = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)
    assert _worker_status_from_heartbeat(None, now) is HealthStatus.UNHEALTHY


def test_worker_status_healthy_when_fresh_and_ok():
    now = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)
    heartbeat = _heartbeat(now + timedelta(seconds=10), WorkerCycleStatus.OK)
    assert _worker_status_from_heartbeat(heartbeat, now) is HealthStatus.HEALTHY


def test_worker_status_unhealthy_when_fresh_but_degraded():
    now = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)
    heartbeat = _heartbeat(now + timedelta(seconds=10), WorkerCycleStatus.DEGRADED)
    assert _worker_status_from_heartbeat(heartbeat, now) is HealthStatus.UNHEALTHY


def test_worker_status_unhealthy_when_heartbeat_expired():
    now = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)
    heartbeat = _heartbeat(now - timedelta(seconds=1), WorkerCycleStatus.OK)
    assert _worker_status_from_heartbeat(heartbeat, now) is HealthStatus.UNHEALTHY


# --- helpers ----------------------------------------------------------------


def _force_workers_healthy(monkeypatch) -> None:
    async def _healthy(worker_name, database):
        return WorkerHealthCheckResponse(worker_name=worker_name, status=HealthStatus.HEALTHY)

    monkeypatch.setattr("app.services.system.health_service.check_worker_health", _healthy)


def _heartbeat(expires_at: datetime, last_status: WorkerCycleStatus) -> WorkerHeartbeat:
    return WorkerHeartbeat(worker_name="housekeeping", expires_at=expires_at, last_status=last_status)


@asynccontextmanager
async def _client(database: _FakeDatabase) -> AsyncIterator[AsyncClient]:
    # Stand in for the shared app-lifespan engine so the real health wiring runs
    # against our fake and we can assert it is never disposed per request (#57).
    app.dependency_overrides[get_database] = lambda: database
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


class _FakeDatabase:
    def __init__(self, *, healthy: bool = True, error: bool = False):
        self._healthy = healthy
        self._error = error
        self.disposed = False

    async def health(self) -> bool:
        if self._error:
            raise RuntimeError("database probe failed")
        return self._healthy

    async def dispose(self) -> None:
        self.disposed = True
