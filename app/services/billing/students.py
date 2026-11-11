"""The student check every billing write shares, read through the CRM's one loading path."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import Party
from app.services.crm.parties import load_party

from .errors import UnknownStudentError


async def require_student(session: AsyncSession, party_id: uuid.UUID) -> None:
    """Raise unless the party holds the student role.

    A missing party raises the CRM's ``PartyNotFoundError`` - the answer to a party addressed in a path; a party
    without the student role raises ``UnknownStudentError``.

    The party is locked first, ``FOR KEY SHARE`` - the lock the billing row's foreign key takes, taken before the
    write - and stays locked until the caller's transaction ends. It and ``delete_party``'s ``FOR UPDATE`` exclude
    each other: a delete holding the party makes this wait and then find no party, instead of the ``RESTRICT``
    foreign key failing the write that follows as a 500; a delete that comes later waits for this transaction and
    answers ``party_in_use``. Nothing else waits: neither another billing write naming the party nor a CRM write
    (``saved`` takes ``FOR NO KEY UPDATE``).
    """
    # read=True: `key_share=True` alone renders FOR NO KEY UPDATE, which would serialize billing and CRM writes.
    await session.execute(select(Party.id).where(Party.id == party_id).with_for_update(read=True, key_share=True))
    party = await load_party(session, party_id)
    if party.person is None or party.person.student is None:
        raise UnknownStudentError(f"Party {party_id} holds no student role")
