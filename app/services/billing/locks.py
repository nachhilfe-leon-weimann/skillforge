"""The lock a billing write takes on a party before a row names it (billing spec, PD9)."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import Party


async def hold_party(session: AsyncSession, party_id: uuid.UUID) -> bool:
    """Lock the party ``FOR KEY SHARE`` until the transaction ends; return whether it exists.

    It is the lock the foreign key takes, taken before the write. It and ``delete_party``'s ``FOR UPDATE`` exclude
    each other: a delete holding the party makes this wait and then find no party, instead of the ``RESTRICT``
    foreign key failing the write that follows as a 500; a delete that comes later waits for this transaction and
    answers ``party_in_use``. Nothing else waits: neither another billing write naming the party nor any other CRM
    write (``saved`` takes ``FOR NO KEY UPDATE``).
    """
    # read=True: `key_share=True` alone renders FOR NO KEY UPDATE, which would serialize billing and CRM writes.
    held = await session.scalar(select(Party.id).where(Party.id == party_id).with_for_update(read=True, key_share=True))
    return held is not None
