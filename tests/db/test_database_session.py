"""`Database.session` begins its transaction at the isolation level asked for - a write's and a read's alike - and
the next session gets the server's default again."""

import pytest
from sqlalchemy import text
from sqlalchemy.engine.interfaces import IsolationLevel

from app.core.db import Database

pytestmark = pytest.mark.db

LEVELS: dict[IsolationLevel, str] = {"REPEATABLE READ": "repeatable read", "SERIALIZABLE": "serializable"}


@pytest.mark.parametrize("write", [True, False], ids=["write", "read"])
@pytest.mark.parametrize("level", list(LEVELS))
async def test_a_session_runs_at_the_isolation_level_asked_for(db_url: str, write: bool, level: IsolationLevel):
    database = Database.from_url(db_url)
    try:
        async with database.session(write=write, isolation_level=level) as session:
            inside = await session.scalar(text("SHOW transaction_isolation"))
        async with database.session(write=write) as session:
            afterwards = await session.scalar(text("SHOW transaction_isolation"))
    finally:
        await database.dispose()

    assert (inside, afterwards) == (LEVELS[level], "read committed")
