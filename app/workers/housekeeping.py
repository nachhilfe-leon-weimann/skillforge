"""SkillForge's housekeeping worker (bot-decoupling spec, P0-3).

Started as the compose service ``worker`` (same image as the app, its own entrypoint, the pooled DB connection) via
``python -m app.workers.housekeeping``. Each cycle runs every pass of ``PASSES`` once - one bounded batch in its own
transaction - then emits one ``housekeeping_cycle`` line and beats for ``/health``. The passes delete with
``SKIP LOCKED``, so several replicas are safe.
"""

import asyncio
import signal
import time
from datetime import timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import Database
from app.core.db.models import WorkerCycleStatus
from app.core.logging import configure_logging, get_logger
from app.services.auth.housekeeping import delete_expired_action_tokens, delete_expired_sessions
from app.services.system import WorkerName, record_worker_heartbeat

HOUSEKEEPING_INTERVAL = timedelta(seconds=30)

# How long a heartbeat stays "fresh" for the health plane: a few cycles, so one slow or missed tick does not flip
# the worker to unhealthy. Derived from the interval, so changing the cadence cannot desync the threshold.
HEARTBEAT_FRESH_FOR = HOUSEKEEPING_INTERVAL * 3

# Rows per pass per cycle: ~2.9 M a day per table, far above what logins produce. One bounded batch keeps every
# cycle - and so the heartbeat and a shutdown - short.
DELETE_BATCH_LIMIT = 1000


class Pass(Protocol):
    async def __call__(self, session: AsyncSession, *, limit: int) -> int: ...


# Log/detail counter -> the pass that fills it. A new table of expiring rows registers its pass here.
PASSES: dict[str, Pass] = {
    "sessions_deleted": delete_expired_sessions,
    "action_tokens_deleted": delete_expired_action_tokens,
}


async def run_cycle(database: Database, logger) -> None:
    """Run every pass once and emit exactly one ``housekeeping_cycle`` line, then beat.

    Each pass runs in its own transaction, so a failing one never rolls back another; it is logged on its own and
    marks the beat ``DEGRADED``. The line is emitted either way: no silent cycle.
    """
    started = time.perf_counter()
    counts = dict.fromkeys(PASSES, 0)
    cycle_ok = True
    for counter, run_pass in PASSES.items():
        try:
            async with database.session() as session:
                counts[counter] = await run_pass(session, limit=DELETE_BATCH_LIMIT)
        except Exception:
            logger.exception("housekeeping_pass_failed", counter=counter)
            cycle_ok = False
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    logger.info("housekeeping_cycle", **counts, duration_ms=duration_ms)

    # Liveness for the health plane, in its own transaction: a missing beat is what marks the worker unhealthy.
    try:
        async with database.session() as session:
            await record_worker_heartbeat(
                session,
                worker_name=WorkerName.HOUSEKEEPING,
                status=WorkerCycleStatus.OK if cycle_ok else WorkerCycleStatus.DEGRADED,
                fresh_for=HEARTBEAT_FRESH_FOR,
                detail={**counts, "duration_ms": duration_ms},
            )
    except Exception:
        logger.exception("housekeeping_heartbeat_failed")


async def run_forever() -> None:
    """Run housekeeping cycles on :data:`HOUSEKEEPING_INTERVAL` until SIGINT/SIGTERM arrives."""
    logger = get_logger(__name__)
    database = Database.from_url(str(get_settings().db.url))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - signal handlers unavailable (e.g. Windows)
            pass

    logger.info("housekeeping_started", interval_s=HOUSEKEEPING_INTERVAL.total_seconds())
    try:
        while not stop.is_set():
            try:
                await run_cycle(database, logger)
            except Exception:
                # run_cycle already handles pass failures internally; this is a backstop for
                # anything unexpected (e.g. logging itself) so one bad tick never stops the loop
                # -- compose would otherwise just restart us into the same state.
                logger.exception("housekeeping_cycle_failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=HOUSEKEEPING_INTERVAL.total_seconds())
            except TimeoutError:
                # Expected: the interval elapsed without a stop signal, so loop into the next
                # cycle. A real shutdown sets `stop` instead, which ends the while loop.
                pass
    finally:
        await database.dispose()
        logger.info("housekeeping_stopped")


def main() -> None:
    configure_logging(get_settings().logging)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
