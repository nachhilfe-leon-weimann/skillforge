import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import UUID, CheckConstraint, DateTime, Enum, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..shared import TimestampMixin
from .base import SystemBase


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Job(TimestampMixin, SystemBase):
    """One unit of SkillForge's own background work (ADR 0011, point 1; calendar-categories spec, decisions B-E).

    The queue's functions are in ``app/services/jobs/queue.py``; the kind is a string to them. Among ``pending`` jobs a
    kind and a ``dedupe_key`` are unique (``uq_job_pending_dedupe``); a NULL key never conflicts. A kind and a key must
    determine the payload: a conflict keeps the pending job's payload, a merge the twin's. ``last_error`` is an error
    class and a status, never payload content.
    """

    __tablename__ = "job"
    __table_args__ = SystemBase.extend_table_args(
        CheckConstraint("attempts >= 0 AND max_attempts >= 1 AND attempts <= max_attempts", name="ck_job_attempts"),
        CheckConstraint("(finished_at IS NOT NULL) = (status IN ('succeeded', 'failed'))", name="ck_job_finished"),
        # A running job without a lease would never be claimed again.
        CheckConstraint("(locked_until IS NOT NULL) = (status = 'running')", name="ck_job_lease"),
        # The claim's two branches: the due pending jobs, and the running jobs whose lease passed. Finished jobs are
        # in neither index, so a claim never reads them.
        Index("ix_job_claimable", "available_at", postgresql_where=text("status = 'pending'")),
        Index("ix_job_lease", "locked_until", postgresql_where=text("status = 'running'")),
        # The arbiter of the queue's `ON CONFLICT`: one pending job per kind and key.
        Index("uq_job_pending_dedupe", "kind", "dedupe_key", unique=True, postgresql_where=text("status = 'pending'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[JobStatus] = mapped_column(
        Enum(
            JobStatus,
            name="job_status",
            schema="system",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
        server_default=JobStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    """Counted by the claim that starts an attempt; ``succeed`` and ``fail`` match it, so a stale holder changes
    nothing."""
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    """When a pending job is due."""
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """The lease of a running job; once it passed, a claim takes the job again."""
    dedupe_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """When the job became ``succeeded`` or ``failed``."""
