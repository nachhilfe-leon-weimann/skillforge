"""Run two overlapping transactions and check the second one waits (the committed-data tests of auth)."""

import asyncio
from collections.abc import Callable, Coroutine

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Database


async def overlapping[T](
    db: Database,
    first_call: Callable[[AsyncSession], Coroutine[object, object, object]],
    second_call: Callable[[AsyncSession], Coroutine[object, object, T]],
) -> asyncio.Task[T]:
    """Run ``first_call`` in a transaction, start ``second_call`` in another, check that it waits, commit the first.

    Returns the second call, finished; its transaction is committed if it succeeded, else rolled back.
    """
    first: AsyncSession = db.session_factory()
    second: AsyncSession = db.session_factory()
    try:
        await first_call(first)
        pending = asyncio.create_task(second_call(second))
        await asyncio.sleep(0.5)
        assert not pending.done(), "the second transaction waits for the first"
        await first.commit()
        await asyncio.wait([pending], timeout=10)
        if pending.exception() is None:
            await second.commit()
        else:
            await second.rollback()
        return pending
    finally:
        await first.close()
        await second.close()
