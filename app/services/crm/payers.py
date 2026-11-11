"""Who pays for a party: the CRM's answer from its ``pays_for`` relations (billing spec, decision I).

A read: it writes nothing and takes no lock. Billing stores what it answers on a lesson (PD1); the CRM keeps no
payer of its own.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import Party, PartyRelation, PartyRelationType

from .errors import PartyNotFoundError


@dataclass(frozen=True)
class PayerResolution:
    """The payer of a student as the CRM holds it now."""

    student_id: uuid.UUID
    payer_id: uuid.UUID | None
    """The one party with a ``pays_for`` relation to the student - a person or a company - or the student itself when
    none has. ``None`` when several have: the payer is ambiguous, and nothing picks one."""

    @property
    def ambiguous(self) -> bool:
        return self.payer_id is None


async def resolve_payer(session: AsyncSession, student_id: uuid.UUID) -> PayerResolution:
    """Resolve who pays for the party: none ``pays_for`` - itself, exactly one - that party, several - ambiguous."""
    if await session.scalar(select(Party.id).where(Party.id == student_id)) is None:
        raise PartyNotFoundError(f"No party with id {student_id}")

    # Two rows are enough to tell one payer from several.
    payer_ids = (
        await session.scalars(
            select(PartyRelation.from_party_id)
            .where(PartyRelation.to_party_id == student_id, PartyRelation.type == PartyRelationType.PAYS_FOR)
            .limit(2)
        )
    ).all()
    match payer_ids:
        case []:
            return PayerResolution(student_id=student_id, payer_id=student_id)
        case [payer_id]:
            return PayerResolution(student_id=student_id, payer_id=payer_id)
        case _:
            return PayerResolution(student_id=student_id, payer_id=None)
