from enum import StrEnum

from pydantic import BaseModel


class HealthStatus(StrEnum):
    HEALTHY = "ok"
    UNHEALTHY = "unhealthy"
    ERROR = "error"

    @property
    def severity(self) -> int:
        return {
            HealthStatus.HEALTHY: 0,
            HealthStatus.UNHEALTHY: 1,
            HealthStatus.ERROR: 2,
        }[self]


class HealthCheckResponse(BaseModel):
    status: HealthStatus


class DependencyName(StrEnum):
    DATABASE = "database"


class DependencyHealthCheckResponse(HealthCheckResponse):
    dependency_name: DependencyName


class DependenciesHealthCheckResponse(HealthCheckResponse):
    checks: dict[DependencyName, HealthStatus]


class WorkerName(StrEnum):
    HOUSEKEEPING = "housekeeping"


class WorkerHealthCheckResponse(HealthCheckResponse):
    worker_name: WorkerName


class WorkersHealthCheckResponse(HealthCheckResponse):
    checks: dict[WorkerName, HealthStatus]


class SystemHealthCheckResponse(HealthCheckResponse):
    version: str
    dependencies: DependenciesHealthCheckResponse
    workers: WorkersHealthCheckResponse
