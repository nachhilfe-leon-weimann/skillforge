from datetime import datetime

from sqlalchemy import func, select

from app.core.db import Database
from app.core.db.models import WorkerCycleStatus, WorkerHeartbeat
from app.core.logging import get_logger

from .heartbeat_service import read_worker_heartbeat
from .schemas import (
    DependenciesHealthCheckResponse,
    DependencyHealthCheckResponse,
    DependencyName,
    HealthStatus,
    SystemHealthCheckResponse,
    WorkerHealthCheckResponse,
    WorkerName,
    WorkersHealthCheckResponse,
)

logger = get_logger(__name__)


def _aggregate_health_status(*statuses: HealthStatus) -> HealthStatus:
    """Worst (highest-severity) status wins; healthy when there is nothing to check."""
    return max(statuses, key=lambda status: status.severity, default=HealthStatus.HEALTHY)


async def check_dependency_health(dependency_name: DependencyName, database: Database) -> DependencyHealthCheckResponse:
    status = HealthStatus.ERROR
    try:
        match dependency_name:
            case DependencyName.DATABASE:
                status = HealthStatus.HEALTHY if await database.health() else HealthStatus.UNHEALTHY
    except Exception:
        logger.exception("Health check failed for dependency %s", dependency_name)
        status = HealthStatus.ERROR

    return DependencyHealthCheckResponse(dependency_name=dependency_name, status=status)


async def check_dependencies_health(database: Database) -> DependenciesHealthCheckResponse:
    """Check every dependency in :class:`DependencyName` and aggregate the overall status."""
    checks = {dependency: (await check_dependency_health(dependency, database)).status for dependency in DependencyName}
    return DependenciesHealthCheckResponse(status=_aggregate_health_status(*checks.values()), checks=checks)


def _worker_status_from_heartbeat(heartbeat: WorkerHeartbeat | None, now: datetime) -> HealthStatus:
    if heartbeat is None:
        return HealthStatus.UNHEALTHY  # never reported in -- not started, or wrong name
    if now >= heartbeat.expires_at:
        return HealthStatus.UNHEALTHY  # beat past its freshness window -- worker dead or hung
    if heartbeat.last_status is WorkerCycleStatus.DEGRADED:
        return HealthStatus.UNHEALTHY  # alive, but its last cycle failed
    return HealthStatus.HEALTHY


async def check_worker_health(worker_name: WorkerName, database: Database) -> WorkerHealthCheckResponse:
    status = HealthStatus.ERROR
    try:
        async with database.session(write=False) as session:
            heartbeat = await read_worker_heartbeat(session, worker_name)
            now = (await session.execute(select(func.now()))).scalar_one()
        status = _worker_status_from_heartbeat(heartbeat, now)
    except Exception:
        logger.exception("Health check failed for worker %s", worker_name)
        status = HealthStatus.ERROR

    return WorkerHealthCheckResponse(worker_name=worker_name, status=status)


async def check_workers_health(database: Database) -> WorkersHealthCheckResponse:
    """Check every worker in :class:`WorkerName` and aggregate the overall status."""
    checks = {worker: (await check_worker_health(worker, database)).status for worker in WorkerName}
    return WorkersHealthCheckResponse(status=_aggregate_health_status(*checks.values()), checks=checks)


async def check_system_health(database: Database, *, version: str) -> SystemHealthCheckResponse:
    """Aggregate dependency and worker health into the overall system status."""
    dependencies = await check_dependencies_health(database)
    workers = await check_workers_health(database)

    return SystemHealthCheckResponse(
        status=_aggregate_health_status(dependencies.status, workers.status),
        version=version,
        dependencies=dependencies,
        workers=workers,
    )
