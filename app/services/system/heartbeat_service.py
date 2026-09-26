from datetime import timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import WorkerCycleStatus, WorkerHeartbeat

from .schemas import WorkerName


async def record_worker_heartbeat(
    session: AsyncSession,
    *,
    worker_name: WorkerName,
    status: WorkerCycleStatus,
    fresh_for: timedelta,
    detail: dict[str, Any] | None = None,
) -> None:
    """Upsert a worker's current heartbeat (one row per ``worker_name``).

    ``last_beat_at`` and ``expires_at`` are stamped from the database clock so liveness
    checks compare against the same clock without app/DB skew. The caller passes
    ``fresh_for`` (its own cadence times a tolerance), so the beat carries its freshness
    window and the health check needs no knowledge of any worker's interval.
    """
    statement = insert(WorkerHeartbeat).values(
        worker_name=worker_name,
        last_beat_at=func.now(),
        expires_at=func.now() + fresh_for,
        last_status=status,
        detail=detail,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[WorkerHeartbeat.worker_name],
            set_={
                "last_beat_at": func.now(),
                "expires_at": func.now() + fresh_for,
                "last_status": statement.excluded.last_status,
                "detail": statement.excluded.detail,
            },
        )
    )


async def read_worker_heartbeat(session: AsyncSession, worker_name: WorkerName) -> WorkerHeartbeat | None:
    return await session.get(WorkerHeartbeat, worker_name)
