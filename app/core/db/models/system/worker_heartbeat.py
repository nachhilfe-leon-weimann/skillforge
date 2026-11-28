from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Enum, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..shared import TimestampMixin
from .base import SystemBase


class WorkerCycleStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"


class WorkerHeartbeat(TimestampMixin, SystemBase):
    """Liveness heartbeat for background workers.

    One row per worker (PK ``worker_name``), upserted on every beat - at the end of every cycle for housekeeping,
    on a fixed interval from a task of its own for the jobs worker.
    The beat carries its own freshness window in ``expires_at``: the writer knows
    its cadence and stamps how long this beat stays valid, so health checks decide
    liveness without knowing any worker's interval (``now() >= expires_at`` => stale).
    ``last_status`` tells a clean cycle from a degraded one. The table records
    *current state*, not a history -- each beat overwrites the previous one.
    """

    __tablename__ = "worker_heartbeat"

    worker_name: Mapped[str] = mapped_column(Text, primary_key=True)
    last_beat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_status: Mapped[WorkerCycleStatus] = mapped_column(
        Enum(
            WorkerCycleStatus,
            name="worker_cycle_status",
            schema="system",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
    )
    # Optional free-form context for the last cycle (e.g. counters, last error).
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
