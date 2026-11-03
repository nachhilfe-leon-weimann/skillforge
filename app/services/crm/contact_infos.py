"""Contact infos: owned children of a party, addressed by their own ID under the party's."""

import uuid
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import ContactInfo, ContactInfoType, Party
from app.core.unset import UNSET, Unset

from .errors import (
    ContactInfoAlreadyExistsError,
    ContactInfoNotFoundError,
    InvalidContactValueError,
    PartyNotFoundError,
)
from .inputs import NewContactInfo, normalize_contact_value
from .parties import saved


async def add_contact_info(
    session: AsyncSession,
    party_id: uuid.UUID,
    *,
    type: ContactInfoType,
    value: str,
    label: str | None = None,
) -> ContactInfo:
    if await session.scalar(select(Party.id).where(Party.id == party_id)) is None:
        raise PartyNotFoundError(f"No party with id {party_id}")

    contact_info = ContactInfo(party_id=party_id, type=type, value=_normalized(type, value), label=label)
    async with _unique_per_party(session):
        session.add(contact_info)

    await saved(session, party_id)
    return contact_info


async def update_contact_info(
    session: AsyncSession,
    party_id: uuid.UUID,
    contact_info_id: uuid.UUID,
    *,
    value: str | None = None,
    label: str | None | Unset = UNSET,
) -> ContactInfo:
    """Change the given fields. ``value=None`` and ``label=UNSET`` mean unchanged; ``label=None`` clears it.

    The type is immutable, so a new value is checked against the stored one. With nothing to change
    the aggregate is not written and ``party.updated_at`` stays put.
    """
    contact_info = await _get_contact_info(session, party_id, contact_info_id)
    if value is None and label is UNSET:
        return contact_info

    new_value = _normalized(contact_info.type, value) if value is not None else None
    async with _unique_per_party(session):
        if new_value is not None:
            contact_info.value = new_value
        if label is not UNSET:
            contact_info.label = label

    await saved(session, party_id)
    return contact_info


async def remove_contact_info(session: AsyncSession, party_id: uuid.UUID, contact_info_id: uuid.UUID) -> None:
    contact_info = await _get_contact_info(session, party_id, contact_info_id)

    await session.delete(contact_info)
    await saved(session, party_id)


def new_contact_infos(contact_infos: Sequence[NewContactInfo]) -> list[ContactInfo]:
    """Build the contact infos a party is created with: normalized, and free of duplicates.

    The request models check both already; this is what holds for in-process callers (ADR 0007).
    """
    rows = [
        ContactInfo(type=info.type, value=_normalized(info.type, info.value), label=info.label)
        for info in contact_infos
    ]
    if len({(row.type, row.value) for row in rows}) != len(rows):
        raise ContactInfoAlreadyExistsError("The same type and value appear twice")
    return rows


def _normalized(type: ContactInfoType, value: str) -> str:
    try:
        return normalize_contact_value(type, value)
    except ValueError:
        # The reason stays out of the chain as well: it is about a personal value.
        raise InvalidContactValueError(f"Not a valid {type.value} value") from None


async def _get_contact_info(session: AsyncSession, party_id: uuid.UUID, contact_info_id: uuid.UUID) -> ContactInfo:
    """Look the contact info up under its party in one query: another party's ID leaks nothing."""
    contact_info = await session.scalar(
        select(ContactInfo)
        .where(ContactInfo.id == contact_info_id, ContactInfo.party_id == party_id)
        .execution_options(populate_existing=True)
    )
    if contact_info is None:
        raise ContactInfoNotFoundError(f"Party {party_id} has no contact info {contact_info_id}")

    return contact_info


@asynccontextmanager
async def _unique_per_party(session: AsyncSession) -> AsyncGenerator[None]:
    """Write the change made inside the block in a SAVEPOINT and translate a uq_contact_info conflict.

    Uniqueness is decided by the constraint, not by check-then-insert; the flush forces the violation
    here instead of at commit. The change must happen *inside* the block: ``begin_nested()`` first
    flushes whatever is pending into the enclosing transaction, where a violation would take the
    whole transaction down - and its message, which names the value, would be logged.
    """
    try:
        async with session.begin_nested():
            yield
            await session.flush()
    except IntegrityError as exc:
        raise ContactInfoAlreadyExistsError("uq_contact_info violated") from exc
