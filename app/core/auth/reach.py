"""Reach: the parties a person restricted to their own data may see (ADR 0008).

A ``:own`` scope restricts its unqualified scope to the caller's reach. ``require_access`` in
``dependencies.py`` turns the token into an ``Access``; a route filters by it and knows nothing of
scopes. HTTP and the CRM's errors stay out of this module.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from types import MappingProxyType
from typing import Self

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import PartyRelation, PartyRelationType

# The relations that let a party act for another (the relation's ``to_party``): the ``guardian`` basis of reach and
# the derived ``guardian`` role (``derive_roles_for`` in ``app/services/auth/roles.py``). ``TUTOR_OF`` is absent on
# purpose: tutoring does not make a guardian.
DELEGATION_RELATION_TYPES = (PartyRelationType.PARENT_OF, PartyRelationType.PAYS_FOR)


class ReachBasis(StrEnum):
    """Why a party is within reach - room for representations that differ by basis."""

    SELF = "self"
    GUARDIAN = "guardian"


@dataclass(frozen=True)
class Access:
    """What a request may see: every party (``Access.all()``) or those within a reach (``Access.of(...)``).

    Built through the two factories only; ``_reach`` is ``None`` for every party.
    """

    _reach: Mapping[uuid.UUID, ReachBasis] | None

    @classmethod
    def all(cls) -> Self:
        return cls(None)

    @classmethod
    def of(cls, reach: Mapping[uuid.UUID, ReachBasis]) -> Self:
        return cls(MappingProxyType(dict(reach)))

    @cached_property
    def party_ids(self) -> frozenset[uuid.UUID] | None:
        """The reachable parties, the filter of a list; ``None`` means every party."""
        return None if self._reach is None else frozenset(self._reach)

    def allows(self, party_id: uuid.UUID) -> bool:
        return self._reach is None or party_id in self._reach

    def basis(self, party_id: uuid.UUID) -> ReachBasis | None:
        """Why ``party_id`` is within reach; ``None`` when it is not, or when access is not restricted."""
        return None if self._reach is None else self._reach.get(party_id)


async def resolve_reach(session: AsyncSession, party_id: uuid.UUID) -> dict[uuid.UUID, ReachBasis]:
    """Return the reach of ``party_id``: itself, and the ``to_party`` of its outgoing delegation relations.

    One query on ``core.party_relation``, run per request, so the reach is always current.
    """
    delegated = await session.scalars(
        select(PartyRelation.to_party_id).where(
            PartyRelation.from_party_id == party_id,
            PartyRelation.type.in_(DELEGATION_RELATION_TYPES),
        )
    )
    return {**dict.fromkeys(delegated, ReachBasis.GUARDIAN), party_id: ReachBasis.SELF}
