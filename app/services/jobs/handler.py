"""What a job's handler is (calendar-categories spec, "Layers"): the worker in ``app/workers/jobs.py`` maps a kind to
one, and a domain that brings a kind brings its handler."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.config import Settings
from app.core.db import Database


@dataclass(frozen=True, slots=True)
class JobContext:
    """What a handler works with. Its writes are its own: it opens its own sessions on ``database``, apart from the
    queue's - the worker records the job's outcome in a transaction of its own afterwards."""

    database: Database
    settings: Settings


class Handler(Protocol):
    """Works one job of its kind. Returning succeeds the job; raising is a failed attempt (decision E), labelled by
    the exception's class and an integer ``status`` it carries, delayed by a ``retry_after`` in whole seconds - never
    by its message.

    A handler never blocks the event loop: sync or CPU-bound work goes to ``asyncio.to_thread``. A blocked loop starves
    the worker's beat, and a stale beat takes the API container down with ``/health``. It lets ``CancelledError``
    propagate - the worker cancels it at its timeout and at a stop - and it is safe to repeat: a stop, a lease that
    passed and a retry all run it again."""

    async def __call__(self, context: JobContext, payload: Mapping[str, Any]) -> None: ...
