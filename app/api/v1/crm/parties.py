from fastapi import APIRouter, status

from app.api.v1.common import DBSession, Page, error_responses
from app.core.auth import Scope, require_scopes
from app.services.crm import parties as parties_service
from app.services.crm.errors import PartyInUseError, PartyNotFoundError

from .params import CrmReadAccess, PartyId, PartyListQuery, VisibleParty
from .schemas import PartyDetail, PartyListItem, party_detail

router = APIRouter(prefix="/parties")


@router.get("")
async def list_parties(params: PartyListQuery, access: CrmReadAccess, session: DBSession) -> Page[PartyListItem]:
    """List and search parties, ordered by name (persons by last name) regardless of case.

    With `crm:read:own` the list holds only the parties within the caller's reach.
    """
    parties, total = await parties_service.list_parties(session, **params.model_dump(), party_ids=access.party_ids)
    return Page.of([PartyListItem.from_model(party) for party in parties], total=total, params=params)


@router.get("/{party_id}", responses=error_responses(PartyNotFoundError))
async def get_party(party_id: VisibleParty, session: DBSession) -> PartyDetail:
    """Read a party - a person or a company - with its roles and contact infos.

    With `crm:read:own` a party out of the caller's reach is `party_not_found`, like a missing one.
    """
    return party_detail(await parties_service.load_party(session, party_id))


@router.delete(
    "/{party_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_scopes(Scope.CRM_WRITE)],
    responses=error_responses(PartyNotFoundError, PartyInUseError),
)
async def delete_party(party_id: PartyId, session: DBSession) -> None:
    """Delete a party together with its roles, contact infos and relations.

    Refused while an active Discord link or an external system (sevDesk, Clockodo, Microsoft) holds the party:
    remove those links first, in the system that owns them. A deactivated Discord link goes with the party.
    """
    await parties_service.delete_party(session, party_id)
