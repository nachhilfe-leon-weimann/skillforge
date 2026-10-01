"""Party relations: associations addressed by their natural key ``from --type--> to``."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import Party, PartyRelation, PartyRelationType, PartyType

from .errors import (
    InvalidPartyRelationError,
    PartyNotFoundError,
    PartyRelationNotFoundError,
    RelatedPartyNotFoundError,
)
from .inputs import RelationDirection
from .parties import PARTY_GRAPH, load_party, saved


@dataclass(frozen=True)
class PartyRelationView:
    """A relation as seen from one of its parties; ``party`` is the other side, loaded through ``PARTY_GRAPH``."""

    type: PartyRelationType
    direction: RelationDirection
    party: Party
    created_at: datetime


async def list_relations(
    session: AsyncSession,
    party_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
    direction: RelationDirection | None = None,
    type: PartyRelationType | None = None,
) -> tuple[list[PartyRelationView], int]:
    """Return one page of the party's relations - both directions unless one is asked for - plus the total."""
    if await session.scalar(select(Party.id).where(Party.id == party_id)) is None:
        raise PartyNotFoundError(f"No party with id {party_id}")

    outgoing = PartyRelation.from_party_id == party_id
    incoming = PartyRelation.to_party_id == party_id
    match direction:
        case RelationDirection.OUTGOING:
            side = outgoing
        case RelationDirection.INCOMING:
            side = incoming
        case None:
            side = or_(outgoing, incoming)
    filters = [side] if type is None else [side, PartyRelation.type == type]

    other_party_id = case((outgoing, PartyRelation.to_party_id), else_=PartyRelation.from_party_id)
    total = await session.scalar(select(func.count()).select_from(PartyRelation).where(*filters))
    relations = (
        await session.scalars(
            select(PartyRelation)
            .where(*filters)
            .order_by(PartyRelation.created_at, PartyRelation.type, other_party_id)
            .limit(limit)
            .offset(offset)
        )
    ).all()

    # One statement for all other sides, whatever the page size.
    other_ids = {_other_side(relation, party_id) for relation in relations}
    others = await session.scalars(
        select(Party).where(Party.id.in_(other_ids)).options(*PARTY_GRAPH).execution_options(populate_existing=True)
    )
    party_by_id = {party.id: party for party in others}
    views = [
        PartyRelationView(
            type=relation.type,
            direction=_direction(relation, party_id),
            party=party_by_id[_other_side(relation, party_id)],
            created_at=relation.created_at,
        )
        for relation in relations
    ]
    return views, total or 0


async def put_relation(
    session: AsyncSession, party_id: uuid.UUID, type: PartyRelationType, to_party_id: uuid.UUID
) -> PartyRelationView:
    """Make sure ``party_id --type--> to_party_id`` exists. Idempotent: an existing relation is left as it is."""
    if type is PartyRelationType.TUTOR_OF:
        await _lock_pair(session, party_id, to_party_id)
    from_party = await load_party(session, party_id)
    try:
        to_party = await load_party(session, to_party_id)
    except PartyNotFoundError:
        raise RelatedPartyNotFoundError(f"No party with id {to_party_id}") from None
    _check_rule(type, from_party, to_party)

    # ON CONFLICT makes the PUT idempotent in one statement, also for two requests racing each other.
    inserted = await session.execute(
        insert(PartyRelation)
        .values(from_party_id=party_id, to_party_id=to_party_id, type=type)
        .on_conflict_do_nothing()
        .returning(PartyRelation.created_at)
    )
    created_at = inserted.scalar_one_or_none()
    if created_at is not None:
        # A relation belongs to both aggregates; an existing one is no write at all.
        await saved(session, party_id, to_party_id)
    else:
        created_at = await session.scalar(
            select(PartyRelation.created_at).where(
                PartyRelation.from_party_id == party_id,
                PartyRelation.to_party_id == to_party_id,
                PartyRelation.type == type,
            )
        )
        assert created_at is not None, "the conflicting relation exists"

    return PartyRelationView(
        type=type,
        direction=RelationDirection.OUTGOING,
        party=await load_party(session, to_party_id),
        created_at=created_at,
    )


async def remove_relation(
    session: AsyncSession, party_id: uuid.UUID, type: PartyRelationType, to_party_id: uuid.UUID
) -> None:
    if type is PartyRelationType.TUTOR_OF:
        await _lock_pair(session, party_id, to_party_id)
    removed = await session.execute(
        delete(PartyRelation)
        .where(
            PartyRelation.from_party_id == party_id,
            PartyRelation.to_party_id == to_party_id,
            PartyRelation.type == type,
        )
        .returning(PartyRelation.from_party_id)
        .execution_options(synchronize_session=False)
    )
    if removed.first() is None:
        raise PartyRelationNotFoundError(f"No {type.value} relation from {party_id} to {to_party_id}")

    await saved(session, party_id, to_party_id)


async def _lock_pair(session: AsyncSession, *party_ids: uuid.UUID) -> None:
    """Lock both parties of a ``tutor_of`` in ID order, before its roles are read or its row is written.

    Every writer of a ``tutor_of`` takes its parties in this one order: ``put_relation`` before its rule reads their
    roles, ``remove_relation`` before it deletes the row, and a role removal (``_lock_with_tutor_of`` in
    ``roles.py``) for the person and the other side of each ``tutor_of`` it takes along (decision O of
    bot-decoupling). Without the lock a PUT could check a role while a removal takes it away and insert its relation
    after the removal cleaned up; in another order two of these writes could deadlock. ``delete_party`` locks only
    the party it deletes first: a race with the deletion of one of the two parties can still deadlock, and Postgres
    then fails one of the two requests.
    """
    await session.execute(
        select(Party.id).where(Party.id.in_(party_ids)).order_by(Party.id).with_for_update(key_share=True)
    )


def _check_rule(type: PartyRelationType, from_party: Party, to_party: Party) -> None:
    """Enforce the relation rules of the CRM API spec. CRM facts only - never Discord state (ADR 0007)."""
    if from_party.id == to_party.id:
        raise _invalid("a party cannot be related to itself")

    match type:
        case PartyRelationType.PARENT_OF:
            if from_party.type is not PartyType.PERSON:
                raise _invalid("parent_of must start at a person")
            if to_party.type is not PartyType.PERSON:
                raise _invalid("parent_of must point to a person")
        case PartyRelationType.TUTOR_OF:
            if from_party.person is None or from_party.person.tutor is None:
                raise _invalid("tutor_of must start at a person holding the tutor role")
            if to_party.person is None or to_party.person.student is None:
                raise _invalid("tutor_of must point to a person holding the student role")
        case PartyRelationType.PAYS_FOR:
            # Anyone may pay: a person or a company.
            if to_party.type is not PartyType.PERSON:
                raise _invalid("pays_for must point to a person")


def _invalid(rule: str) -> InvalidPartyRelationError:
    return InvalidPartyRelationError(f"{InvalidPartyRelationError.message}: {rule}")


def _direction(relation: PartyRelation, party_id: uuid.UUID) -> RelationDirection:
    return RelationDirection.OUTGOING if relation.from_party_id == party_id else RelationDirection.INCOMING


def _other_side(relation: PartyRelation, party_id: uuid.UUID) -> uuid.UUID:
    return relation.to_party_id if relation.from_party_id == party_id else relation.from_party_id
