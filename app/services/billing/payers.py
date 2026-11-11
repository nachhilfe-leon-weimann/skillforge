"""Who pays for a lesson (billing spec, decision I and PD1), where its invoice goes (decision J), and who is billed by
hand (PD5).

The payer is the CRM's (``resolve_payer``) and is stored on a lesson when it becomes billable, so a later change of
``pays_for`` leaves the lesson with the payer it had; ``lessons.py`` decides when, this module what. It is the one
writer of ``ext.sevdesk_contact`` (architecture test) and of ``billing.manual_payer``. A contact id is stored as
given: nothing here calls sevDesk, and no message repeats a contact id.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import Lesson, ManualPayer, Party, SevdeskContact
from app.services.crm.errors import PartyNotFoundError
from app.services.crm.payers import PayerResolution, resolve_payer

from .errors import LessonNotFoundError, SevdeskContactAlreadyLinkedError


@dataclass(frozen=True)
class StudentPayer:
    """Who pays for a student now, and how that payer is billed."""

    resolution: PayerResolution
    sevdesk_contact_linked: bool
    """Whether the payer has a sevDesk contact; ``False`` while the payer is ambiguous."""
    manual_billing: bool
    """Whether the payer is billed by hand; ``False`` while the payer is ambiguous."""


async def get_student_payer(session: AsyncSession, student_id: uuid.UUID) -> StudentPayer:
    """Resolve the party's payer as a lesson becoming billable now would store it.

    The party must exist, not hold the student role: a former student's lessons are still billed.
    """
    resolution = await resolve_payer(session, student_id)
    if resolution.payer_id is None:
        return StudentPayer(resolution, sevdesk_contact_linked=False, manual_billing=False)

    linked, manual = (
        await session.execute(
            select(
                exists().where(SevdeskContact.party_id == resolution.payer_id),
                exists().where(ManualPayer.party_id == resolution.payer_id),
            )
        )
    ).one()
    return StudentPayer(resolution, sevdesk_contact_linked=linked, manual_billing=manual)


async def lesson_payer(session: AsyncSession, *, student_id: uuid.UUID, billable: bool) -> uuid.UUID | None:
    """The payer to store on a lesson of the student (PD1): the CRM's payer, while the lesson is billable.

    ``None`` for a lesson that is not billable - it has no payer - and for a student with several payers: the tick is
    never refused, and the preview blocks a billable lesson without a payer instead of picking one.
    """
    if not billable:
        return None

    return (await resolve_payer(session, student_id)).payer_id


async def resolve_lesson_payer(session: AsyncSession, lesson_id: uuid.UUID) -> Lesson:
    """Resolve the payer of a lesson afresh and store it: the maintainer's choice when the CRM's payer changed (PD1).

    A lesson that is not billable keeps no payer, so nothing changes for it.
    """
    lesson = await session.get(Lesson, lesson_id, with_for_update=True, populate_existing=True)
    if lesson is None:
        raise LessonNotFoundError(f"No lesson with id {lesson_id}")

    lesson.payer_party_id = await lesson_payer(session, student_id=lesson.student_party_id, billable=lesson.billable)
    await session.flush()
    return lesson


async def link_sevdesk_contact(session: AsyncSession, party_id: uuid.UUID, sevdesk_id: str) -> SevdeskContact:
    """Link the party to its sevDesk contact. Idempotent: the same link again changes nothing.

    A contact linked to another party, or another contact for this party, is ``SevdeskContactAlreadyLinkedError``.
    One statement decides, so two racing requests cannot both link, and neither fails on a unique constraint.
    """
    await _hold_party(session, party_id)

    linked = (
        await session.scalars(
            insert(SevdeskContact)
            .values(sevdesk_id=sevdesk_id, party_id=party_id)
            .on_conflict_do_nothing()
            .returning(SevdeskContact),
            execution_options={"populate_existing": True},
        )
    ).one_or_none()
    if linked is not None:
        return linked

    # Something conflicted: either exactly this link exists - nothing to do - or another one stands in the way.
    existing = await session.scalar(
        select(SevdeskContact).where(SevdeskContact.sevdesk_id == sevdesk_id, SevdeskContact.party_id == party_id)
    )
    if existing is None:
        raise SevdeskContactAlreadyLinkedError(f"Party {party_id} or the contact is linked elsewhere")

    return existing


async def unlink_sevdesk_contact(session: AsyncSession, party_id: uuid.UUID) -> None:
    """Remove the party's sevDesk contact link; a party without one is left as it is."""
    await _hold_party(session, party_id)
    await session.execute(delete(SevdeskContact).where(SevdeskContact.party_id == party_id))


async def put_manual_payer(session: AsyncSession, party_id: uuid.UUID, *, note: str | None = None) -> ManualPayer:
    """Mark the party as billed by hand, or replace the note of its mark. The same note again changes nothing."""
    await _hold_party(session, party_id)

    mark = insert(ManualPayer).values(party_id=party_id, note=note)
    written = (
        await session.scalars(
            mark.on_conflict_do_update(
                index_elements=[ManualPayer.party_id],
                # ON CONFLICT runs no Python-side `onupdate`, so the timestamp is set here.
                set_={"note": note, "updated_at": func.now()},
                where=ManualPayer.note.is_distinct_from(mark.excluded.note),
            ).returning(ManualPayer),
            execution_options={"populate_existing": True},
        )
    ).one_or_none()
    if written is not None:
        return written

    # The note did not differ, so nothing was written: the mark is as the request wants it.
    return (await session.scalars(select(ManualPayer).where(ManualPayer.party_id == party_id))).one()


async def delete_manual_payer(session: AsyncSession, party_id: uuid.UUID) -> None:
    """Unmark the party; a party that is not marked is left as it is."""
    await _hold_party(session, party_id)
    await session.execute(delete(ManualPayer).where(ManualPayer.party_id == party_id))


async def _hold_party(session: AsyncSession, party_id: uuid.UUID) -> None:
    """Make sure the party exists and keep it until the request ends.

    ``FOR KEY SHARE`` is the lock a foreign key takes: a concurrent ``delete_party`` waits and then its guard sees the
    row written here, and a party deleted first is ``PartyNotFoundError`` - never a foreign-key violation.
    """
    held = await session.scalar(select(Party.id).where(Party.id == party_id).with_for_update(key_share=True))
    if held is None:
        raise PartyNotFoundError(f"No party with id {party_id}")
