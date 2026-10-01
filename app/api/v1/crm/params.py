"""Path and query vocabulary of the CRM routes; endpoint signatures use these aliases only."""

import uuid
from typing import Annotated

from fastapi import Depends, Path, Query
from pydantic import AfterValidator, Field

from app.api.v1.common import PageParams, UpdatedSince
from app.core.auth import Access, Scope, require_access
from app.core.db.models import PartyRelationType, PartyType
from app.services.crm.errors import PartyNotFoundError
from app.services.crm.inputs import PartyRole, RelationDirection, require_storable_text

PartyId = Annotated[
    uuid.UUID,
    Path(description="ID of the party.", examples=["7d9f4f3e-1c2b-4a5d-9e8f-0a1b2c3d4e5f"]),
]

# The parties a reader may see: all with `crm:read`, those within their reach with `crm:read:own`.
CrmReadAccess = Annotated[Access, require_access(Scope.CRM_READ)]


async def _visible_party(party_id: PartyId, access: CrmReadAccess) -> uuid.UUID:
    """A party out of reach reads exactly like a missing one, so a restricted reader cannot probe for parties."""
    if not access.allows(party_id):
        raise PartyNotFoundError(f"No party with id {party_id}")

    return party_id


# `PartyId` of a reach-aware route: raises `PartyNotFoundError` for a party out of the reader's reach.
VisibleParty = Annotated[uuid.UUID, Depends(_visible_party)]

ToPartyId = Annotated[
    uuid.UUID,
    Path(
        description="ID of the party the relation points to, e.g. the child of `parent_of`.",
        examples=["1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"],
    ),
]

RelationType = Annotated[
    PartyRelationType,
    Path(
        description="Type of the relation, read from the first party: `{party_id}` is `parent_of` `{to_party_id}`.",
        examples=[PartyRelationType.PARENT_OF],
    ),
]

ContactInfoId = Annotated[
    uuid.UUID,
    Path(
        description="ID of the contact info, as listed in the party detail.",
        examples=["3f2b8c1e-5a4d-4e6f-8a9b-0c1d2e3f4a5b"],
    ),
]

# Where a created party can be read: the target of the `Location` header of `POST /persons` and `POST /companies`.
PARTY_LOCATION = "/api/v1/crm/parties/{party_id}"

# core.subject.id is a Postgres INTEGER: a larger value could not be bound to the query.
MAX_SUBJECT_ID = 2**31 - 1

SubjectId = Annotated[
    int,
    Path(ge=1, le=MAX_SUBJECT_ID, description="ID of the subject.", examples=[1]),
]


# Every word of `q` becomes four bind parameters, and a statement takes 32767 of them.
MAX_SEARCH_LENGTH = 200


class PartyListParams(PageParams):
    """Filters of `GET /parties`. They never fail: a combination nothing matches is an empty page."""

    type: PartyType | None = Field(None, description="Only parties of this type.")
    role: PartyRole | None = Field(None, description="Only persons holding this role.")
    subject_id: int | None = Field(
        None,
        ge=1,
        le=MAX_SUBJECT_ID,
        description="Only persons having this subject - in the given `role`, or in any role without one.",
    )
    q: Annotated[str, AfterValidator(require_storable_text)] | None = Field(
        None,
        min_length=2,
        max_length=MAX_SEARCH_LENGTH,
        description=(
            "Search text, split on whitespace. Every word must appear, ignoring case, in the first name, the last "
            "name, the company name or a contact value. Searching for an e-mail address doubles as the duplicate "
            "check before creating a party. A phone number is stored in E.164 (`+491711234567`), so search for its "
            "digits without the national leading zero: `171 1234567`."
        ),
    )
    updated_since: UpdatedSince = None


type PartyListQuery = Annotated[PartyListParams, Query()]


class RelationListParams(PageParams):
    """Filters of `GET /parties/{party_id}/relations`."""

    direction: RelationDirection | None = Field(
        None,
        description=(
            "`outgoing`: only relations starting at this party; `incoming`: only those pointing to it. Both by default."
        ),
    )
    type: PartyRelationType | None = Field(None, description="Only relations of this type.")


type RelationListQuery = Annotated[RelationListParams, Query()]
