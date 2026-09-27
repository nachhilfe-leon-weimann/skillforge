from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import insert


def test_worker_heartbeat_table_shape():
    from app.core.db.models import WorkerHeartbeat

    table = cast(Table, WorkerHeartbeat.__table__)

    assert table.schema == "system"
    assert table.name == "worker_heartbeat"
    assert [column.name for column in table.primary_key.columns] == ["worker_name"]
    assert table.c.last_beat_at.nullable is False
    assert table.c.expires_at.nullable is False
    assert table.c.last_status.nullable is False
    assert table.c.detail.nullable is True


@pytest.mark.db
async def test_worker_heartbeat_upsert_keeps_one_row_per_worker(session):
    from app.core.db.models import WorkerCycleStatus, WorkerHeartbeat

    first_beat = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)
    await session.execute(_beat("housekeeping", first_beat, WorkerCycleStatus.OK))

    second_beat = first_beat + timedelta(seconds=30)
    await session.execute(_beat("housekeeping", second_beat, WorkerCycleStatus.DEGRADED))
    await session.flush()

    rows = (await session.execute(select(WorkerHeartbeat))).scalars().all()

    assert len(rows) == 1
    assert rows[0].worker_name == "housekeeping"
    assert rows[0].last_beat_at == second_beat
    assert rows[0].last_status is WorkerCycleStatus.DEGRADED


@pytest.mark.db
async def test_worker_heartbeat_tracks_workers_independently(session):
    from app.core.db.models import WorkerCycleStatus, WorkerHeartbeat

    beat_at = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)
    await session.execute(_beat("housekeeping", beat_at, WorkerCycleStatus.OK))
    await session.execute(_beat("future-sync-worker", beat_at, WorkerCycleStatus.OK))
    await session.flush()

    result = await session.execute(select(WorkerHeartbeat.worker_name).order_by(WorkerHeartbeat.worker_name))
    names = result.scalars().all()

    assert names == ["future-sync-worker", "housekeeping"]


def _beat(worker_name, last_beat_at, last_status):
    from app.core.db.models import WorkerHeartbeat

    statement = insert(WorkerHeartbeat).values(
        worker_name=worker_name,
        last_beat_at=last_beat_at,
        expires_at=last_beat_at + timedelta(seconds=90),
        last_status=last_status,
    )
    return statement.on_conflict_do_update(
        index_elements=[WorkerHeartbeat.worker_name],
        set_={
            "last_beat_at": statement.excluded.last_beat_at,
            "expires_at": statement.excluded.expires_at,
            "last_status": statement.excluded.last_status,
        },
    )
