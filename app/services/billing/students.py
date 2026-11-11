"""The student check every billing write shares, read through the CRM's one loading path."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.crm.errors import PartyNotFoundError
from app.services.crm.parties import load_party

from .errors import UnknownStudentError
from .locks import hold_party


async def require_student(session: AsyncSession, party_id: uuid.UUID) -> None:
    """Raise unless the party holds the student role.

    A missing party raises the CRM's ``PartyNotFoundError`` - the answer to a party addressed in a path; a party
    without the student role raises ``UnknownStudentError``.

    The party is locked first (``hold_party``, ``FOR KEY SHARE``) and stays locked until the caller's transaction
    ends, so a concurrent ``delete_party`` answers a domain error, never a 500, and no other CRM write waits for it.
    """
    await hold_party(session, party_id)
    party = await load_party(session, party_id)
    if party.person is None or party.person.student is None:
        raise UnknownStudentError(f"Party {party_id} holds no student role")


async def require_named_student(session: AsyncSession, party_id: uuid.UUID) -> None:
    """``require_student`` for a student named in a body: a party that does not exist is no student either (422, not
    404)."""
    try:
        await require_student(session, party_id)
    except PartyNotFoundError:
        raise UnknownStudentError(f"No party with id {party_id}") from None
