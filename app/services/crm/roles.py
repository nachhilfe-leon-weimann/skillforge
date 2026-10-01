"""The student and tutor roles of a person: idempotent singletons keyed by the person's party ID."""

import uuid
from collections.abc import Callable, Set

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import (
    Party,
    PartyRelation,
    PartyRelationType,
    Person,
    PreferredMeetingTool,
    Student,
    StudentSubject,
    Tutor,
    TutorSubject,
)

from .errors import PartyNotFoundError, PersonNotFoundError, RoleNotFoundError
from .inputs import RelationDirection
from .parties import load_party, saved
from .subjects import require_subjects


async def put_student_role(
    session: AsyncSession,
    party_id: uuid.UUID,
    *,
    preferred_meeting_tool: PreferredMeetingTool,
    subject_ids: Set[int] = frozenset(),
) -> Party:
    """Give the person the student role or replace its data; ``subject_ids`` replaces the whole set."""
    person = await _load_person(session, party_id)
    await require_subjects(session, subject_ids)

    changed = apply_student_role(person, preferred_meeting_tool=preferred_meeting_tool, subject_ids=subject_ids)
    return await _finish(session, party_id, changed=changed)


async def put_tutor_role(session: AsyncSession, party_id: uuid.UUID, *, subject_ids: Set[int] = frozenset()) -> Party:
    """Give the person the tutor role or replace its data; ``subject_ids`` replaces the whole set."""
    person = await _load_person(session, party_id)
    await require_subjects(session, subject_ids)

    changed = apply_tutor_role(person, subject_ids=subject_ids)
    return await _finish(session, party_id, changed=changed)


async def remove_student_role(session: AsyncSession, party_id: uuid.UUID) -> Party:
    """Take the student role away, and with it every ``tutor_of`` pointing to the person; its tutors move too.

    A ``tutor_of`` stands on both roles, so it goes with either (decision O of bot-decoupling). CRM rules only:
    Discord state is not looked at (ADR 0007).
    """
    await _lock_with_tutor_of(session, party_id, RelationDirection.INCOMING)
    person = await _load_person(session, party_id)
    if person.student is None:
        raise RoleNotFoundError(f"Person {party_id} is not a student")

    person.student = None
    tutors = await _remove_tutor_of(session, party_id, RelationDirection.INCOMING)
    await saved(session, party_id, *tutors)
    return await load_party(session, party_id)


async def remove_tutor_role(session: AsyncSession, party_id: uuid.UUID) -> Party:
    """Take the tutor role away, and with it every ``tutor_of`` starting at the person; its students move too.

    A ``tutor_of`` stands on both roles, so it goes with either (decision O of bot-decoupling). CRM rules only:
    Discord state is not looked at (ADR 0007).
    """
    await _lock_with_tutor_of(session, party_id, RelationDirection.OUTGOING)
    person = await _load_person(session, party_id)
    if person.tutor is None:
        raise RoleNotFoundError(f"Person {party_id} is not a tutor")

    person.tutor = None
    students = await _remove_tutor_of(session, party_id, RelationDirection.OUTGOING)
    await saved(session, party_id, *students)
    return await load_party(session, party_id)


def apply_student_role(person: Person, *, preferred_meeting_tool: PreferredMeetingTool, subject_ids: Set[int]) -> bool:
    """Set the role on a person whose roles are loaded (or who is new); return whether anything changed.

    The caller has checked the subjects and ends the write with ``saved`` and ``load_party``.
    """
    student = person.student
    if student is None:
        person.student = Student(
            preferred_meeting_tool=preferred_meeting_tool,
            student_subjects=[StudentSubject(subject_id=subject_id) for subject_id in sorted(subject_ids)],
        )
        return True

    changed = _sync_subjects(
        student.student_subjects, subject_ids, lambda subject_id: StudentSubject(subject_id=subject_id)
    )
    if student.preferred_meeting_tool != preferred_meeting_tool:
        student.preferred_meeting_tool = preferred_meeting_tool
        changed = True
    return changed


def apply_tutor_role(person: Person, *, subject_ids: Set[int]) -> bool:
    """Set the role on a person whose roles are loaded (or who is new); return whether anything changed."""
    tutor = person.tutor
    if tutor is None:
        person.tutor = Tutor(tutor_subjects=[TutorSubject(subject_id=subject_id) for subject_id in sorted(subject_ids)])
        return True

    return _sync_subjects(tutor.tutor_subjects, subject_ids, lambda subject_id: TutorSubject(subject_id=subject_id))


def _sync_subjects[Link: (StudentSubject, TutorSubject)](
    links: list[Link], subject_ids: Set[int], new_link: Callable[[int], Link]
) -> bool:
    """Add the missing rows and delete the surplus ones; return whether the set changed.

    The difference is what tells a write from a PUT that changes nothing. (For the rows themselves
    it makes no difference to reassigning the loaded collection: SQLAlchemy turns a deleted and a
    pending object with the same key into no statement at all.)
    """
    current = {link.subject_id: link for link in links}
    surplus = current.keys() - subject_ids
    missing = subject_ids - current.keys()
    for subject_id in surplus:
        links.remove(current[subject_id])
    for subject_id in sorted(missing):
        links.append(new_link(subject_id))
    return bool(surplus or missing)


async def _load_person(session: AsyncSession, party_id: uuid.UUID) -> Person:
    """Load the person through the one loading path; a company's ID does not exist from here.

    The party row is locked first. Without it two overlapping PUTs of the same role - a client
    retrying - would both find no role and both insert it, and the loser's primary-key violation
    would be a 500. With it the second request waits, sees the role and changes nothing. It is the
    lock ``saved`` takes anyway (``FOR NO KEY UPDATE``), so linking and relating are not blocked.
    """
    await session.execute(select(Party.id).where(Party.id == party_id).with_for_update(key_share=True))
    try:
        party = await load_party(session, party_id)
    except PartyNotFoundError:
        raise PersonNotFoundError(f"No person with party id {party_id}") from None
    if party.person is None:
        raise PersonNotFoundError(f"No person with party id {party_id}")

    return party.person


async def _lock_with_tutor_of(session: AsyncSession, party_id: uuid.UUID, direction: RelationDirection) -> None:
    """Lock the person and the other side of each of their ``tutor_of`` in ``direction``: one set, in ID order.

    The order in which ``put_relation`` and ``remove_relation`` lock the pair of a ``tutor_of`` (``_lock_pair`` in
    ``relations.py``). Locking the person first and the other side only when ``saved`` moves it would deadlock with a
    PUT or DELETE of that pair that locked the other side first. ``_load_person`` locks the person again - a no-op.
    A ``tutor_of`` committed while this statement waits for the person is not in the set: ``_remove_tutor_of`` still
    removes it, and ``saved`` locks its other side last - a deadlock only with a third request that holds that party.
    A concurrent ``delete_party`` of one of those other sides can deadlock with it too: ``delete_party`` locks its party
    first and the related parties only in ``saved`` - a 500 for one of the two.
    """
    match direction:
        case RelationDirection.OUTGOING:
            others = select(PartyRelation.to_party_id).where(PartyRelation.from_party_id == party_id)
        case RelationDirection.INCOMING:
            others = select(PartyRelation.from_party_id).where(PartyRelation.to_party_id == party_id)
    tutor_of = others.where(PartyRelation.type == PartyRelationType.TUTOR_OF)
    await session.execute(
        select(Party.id)
        .where(or_(Party.id == party_id, Party.id.in_(tutor_of)))
        .order_by(Party.id)
        .with_for_update(key_share=True)
    )


async def _remove_tutor_of(session: AsyncSession, party_id: uuid.UUID, direction: RelationDirection) -> list[uuid.UUID]:
    """Delete the person's ``tutor_of`` relations in ``direction``; return the parties on their other side."""
    match direction:
        case RelationDirection.OUTGOING:
            own, other = PartyRelation.from_party_id, PartyRelation.to_party_id
        case RelationDirection.INCOMING:
            own, other = PartyRelation.to_party_id, PartyRelation.from_party_id
    removed = await session.scalars(
        delete(PartyRelation)
        .where(own == party_id, PartyRelation.type == PartyRelationType.TUTOR_OF)
        .returning(other)
        .execution_options(synchronize_session=False)
    )
    return list(removed)


async def _finish(session: AsyncSession, party_id: uuid.UUID, *, changed: bool) -> Party:
    # A PUT that changes nothing is not a write: updated_at stays, and the answer repeats exactly.
    if changed:
        await saved(session, party_id)
    return await load_party(session, party_id)
