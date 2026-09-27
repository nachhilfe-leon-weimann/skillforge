"""The party aggregate: its one loading path and the bookkeeping every write ends with."""

import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, delete, exists, func, literal, or_, select, true, union_all, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db.models import (
    ClockodoCustomer,
    ClockodoProject,
    Company,
    ContactInfo,
    DiscordAccount,
    MicrosoftAccount,
    MicrosoftContact,
    Party,
    PartyRelation,
    PartyType,
    Person,
    SevdeskContact,
    Student,
    StudentSubject,
    Tutor,
    TutorSubject,
)

from .errors import PartyInUseError, PartyNotFoundError
from .inputs import PartyRole

# Everything a party representation may touch. Async SQLAlchemy cannot lazy-load, so a mapper that
# needs more extends this tuple instead of loading on its own.
PARTY_GRAPH = (
    selectinload(Party.person)
    .selectinload(Person.student)
    .selectinload(Student.student_subjects)
    .selectinload(StudentSubject.subject),
    selectinload(Party.person)
    .selectinload(Person.tutor)
    .selectinload(Tutor.tutor_subjects)
    .selectinload(TutorSubject.subject),
    selectinload(Party.company),
    selectinload(Party.contact_infos),
)


# What keeps a party from being deleted, by the name a client sees, and when a row still counts. The Discord
# table belongs to auth (ADR 0009) - a deactivated link is history that keeps no party alive; the other tables
# belong to the integrations (ADR 0007). The CRM reads them here and nowhere else, and never writes them.
EXTERNAL_LINKS = (
    ("discord_account", DiscordAccount, DiscordAccount.active.is_(true())),
    ("sevdesk_contact", SevdeskContact, true()),
    ("clockodo_customer", ClockodoCustomer, true()),
    ("clockodo_project", ClockodoProject, true()),
    ("microsoft_account", MicrosoftAccount, true()),
    ("microsoft_contact", MicrosoftContact, true()),
)

# Sorts persons ("Mustermann Max") and companies into one case-insensitive order.
_SORT_NAME = func.lower(func.coalesce(Company.name, Person.lastname + " " + Person.firstname))


async def load_party(session: AsyncSession, party_id: uuid.UUID) -> Party:
    """Load the party with its whole graph, refreshing whatever the session already holds."""
    result = await session.execute(
        select(Party).where(Party.id == party_id).options(*PARTY_GRAPH).execution_options(populate_existing=True)
    )
    party = result.scalar_one_or_none()
    if party is None:
        raise PartyNotFoundError(f"No party with id {party_id}")

    return party


async def saved(session: AsyncSession, *party_ids: uuid.UUID) -> None:
    """Mark the aggregates as changed and write everything that is pending.

    ``Party`` is the aggregate root: its ``updated_at`` is the one change signal consumers get, so
    every write inside the aggregate ends here. A party created in the same unit of work is not in
    the database yet; its ``updated_at`` comes from the column default on the flush below.
    """
    await session.execute(
        update(Party)
        .where(Party.id.in_(party_ids))
        .values(updated_at=func.now())
        # load_party() refreshes the instances afterwards, so nothing needs to be synchronized here.
        .execution_options(synchronize_session=False)
    )
    await session.flush()


async def list_parties(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    type: PartyType | None = None,
    role: PartyRole | None = None,
    subject_id: int | None = None,
    q: str | None = None,
    updated_since: datetime | None = None,
    party_ids: frozenset[uuid.UUID] | None = None,
) -> tuple[list[Party], int]:
    """Return one page of parties matching all given filters, plus the size of the filtered set.

    Filters never fail: a combination nothing can match (a company holding a role, an unknown
    subject) is an empty page. ``party_ids`` confines the list to those parties - a reader's reach;
    ``None`` lists them all.
    """
    filtered = (
        select(Party)
        .outerjoin(Person, Person.party_id == Party.id)
        .outerjoin(Company, Company.party_id == Party.id)
        .where(
            *_filters(
                type=type, role=role, subject_id=subject_id, q=q, updated_since=updated_since, party_ids=party_ids
            )
        )
    )
    total = await session.scalar(select(func.count()).select_from(filtered.subquery()))
    result = await session.execute(
        filtered
        .options(*PARTY_GRAPH)
        .order_by(_SORT_NAME, Party.id)
        .limit(limit)
        .offset(offset)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars()), total or 0


async def delete_party(session: AsyncSession, party_id: uuid.UUID) -> None:
    """Delete a party with its roles, contact infos and relations - unless an external system knows it."""
    # Every write that leaves a link active locks its party first (auth's Discord links explicitly, the other links
    # through their foreign key), so holding this lock means no active link can appear or come back between the check
    # and the delete: every foreign key into core.party cascades, and such a link would silently vanish with the party.
    locked = await session.scalar(select(Party.id).where(Party.id == party_id).with_for_update())
    if locked is None:
        raise PartyNotFoundError(f"No party with id {party_id}")

    linked = await _external_link_kinds(session, party_id)
    if linked:
        raise PartyInUseError(f"{PartyInUseError.message}: {', '.join(linked)}")

    # The relations cascade away, which changes the aggregate of the party on their other side.
    related = await session.scalars(
        select(PartyRelation.to_party_id)
        .where(PartyRelation.from_party_id == party_id)
        .union(select(PartyRelation.from_party_id).where(PartyRelation.to_party_id == party_id))
    )
    related_ids = [related_id for related_id in related if related_id != party_id]

    await session.execute(delete(Party).where(Party.id == party_id).execution_options(synchronize_session=False))
    await saved(session, *related_ids)


def _filters(
    *,
    type: PartyType | None,
    role: PartyRole | None,
    subject_id: int | None,
    q: str | None,
    updated_since: datetime | None,
    party_ids: frozenset[uuid.UUID] | None,
) -> list[ColumnElement[bool]]:
    filters: list[ColumnElement[bool]] = []
    if party_ids is not None:
        filters.append(Party.id.in_(party_ids))

    if type is not None:
        filters.append(Party.type == type)

    # The boundary is included: a consumer polling for changes sees one twice rather than never.
    if updated_since is not None:
        filters.append(Party.updated_at >= updated_since)

    is_student = exists().where(Student.person_id == Party.id)
    is_tutor = exists().where(Tutor.person_id == Party.id)
    if role is not None:
        filters.append(is_student if role is PartyRole.STUDENT else is_tutor)

    if subject_id is not None:
        learns = exists().where(StudentSubject.student_id == Party.id, StudentSubject.subject_id == subject_id)
        teaches = exists().where(TutorSubject.tutor_id == Party.id, TutorSubject.subject_id == subject_id)
        match role:
            case PartyRole.STUDENT:
                filters.append(learns)
            case PartyRole.TUTOR:
                filters.append(teaches)
            case None:
                filters.append(or_(learns, teaches))

    # Every token must match somewhere; autoescape keeps "%" and "_" literal.
    for token in (q or "").split():
        filters.append(
            or_(
                Person.firstname.icontains(token, autoescape=True),
                Person.lastname.icontains(token, autoescape=True),
                Company.name.icontains(token, autoescape=True),
                exists().where(ContactInfo.party_id == Party.id, ContactInfo.value.icontains(token, autoescape=True)),
            )
        )

    return filters


async def _external_link_kinds(session: AsyncSession, party_id: uuid.UUID) -> list[str]:
    """Return the kinds of external links still holding the party, in the order of ``EXTERNAL_LINKS``."""
    found = await session.scalars(
        union_all(
            *(
                select(literal(kind)).where(exists().where(model.party_id == party_id, still_linked))
                for kind, model, still_linked in EXTERNAL_LINKS
            )
        )
    )
    present = set(found)
    return [kind for kind, _, _ in EXTERNAL_LINKS if kind in present]
