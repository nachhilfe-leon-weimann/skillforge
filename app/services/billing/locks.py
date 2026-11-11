"""The lock a billing write takes on a party before a row names it (billing spec, PD9)."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import Party


async def hold_party(session: AsyncSession, party_id: uuid.UUID) -> bool:
    """Lock the party ``FOR KEY SHARE`` until the transaction ends; return whether it exists.

    It is the lock the foreign key takes, taken before the write: a ``delete_party`` holding the party (``FOR
    UPDATE``) makes this wait and then find no party, instead of the ``RESTRICT`` foreign key failing the write that
    follows as a 500; one that comes later waits for this transaction and answers ``party_in_use``. CRM writes are
    not blocked (``saved`` takes ``FOR NO KEY UPDATE``).
    """
    held = await session.scalar(select(Party.id).where(Party.id == party_id).with_for_update(key_share=True))
    return held is not None
