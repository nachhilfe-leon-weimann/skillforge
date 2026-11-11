from fastapi import APIRouter, status

from app.api.v1.common import DBSession, Page, PageQuery, error_responses
from app.core.auth import Scope, require_scopes
from app.services.billing import rates as rates_service
from app.services.billing.errors import RateNotFoundError, RateNotQuarterHourError, UnknownStudentError
from app.services.crm.errors import PartyNotFoundError

from .params import StudentId, ValidFrom
from .schemas import RatePutRequest, RateResponse

router = APIRouter(prefix="/students/{party_id}/rates")


@router.get(
    "",
    dependencies=[require_scopes(Scope.BILLING_READ)],
    responses=error_responses(PartyNotFoundError),
)
async def list_rates(party_id: StudentId, params: PageQuery, session: DBSession) -> Page[RateResponse]:
    """List a student's own rates, newest day first. Each holds from its day until the next one's."""
    rates, total = await rates_service.list_rates(session, party_id, limit=params.limit, offset=params.offset)
    return Page.of([RateResponse.from_model(rate) for rate in rates], total=total, params=params)


@router.put(
    "/{valid_from}",
    dependencies=[require_scopes(Scope.BILLING_WRITE)],
    responses=error_responses(PartyNotFoundError, UnknownStudentError, RateNotQuarterHourError),
)
async def put_rate(
    party_id: StudentId, valid_from: ValidFrom, request: RatePutRequest, session: DBSession
) -> RateResponse:
    """Set the student's rate from that day on. Idempotent; a rate already set for the day is replaced.

    A charge keeps the rate it was priced with: a new rate changes no invoice position.
    """
    return RateResponse.from_model(await rates_service.put_rate(session, party_id, valid_from, **request.model_dump()))


@router.delete(
    "/{valid_from}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_scopes(Scope.BILLING_WRITE)],
    responses=error_responses(PartyNotFoundError, RateNotFoundError),
)
async def delete_rate(party_id: StudentId, valid_from: ValidFrom, session: DBSession) -> None:
    """Remove the rate of that day; the rate before it then holds on. A charge keeps the rate it was priced with."""
    await rates_service.delete_rate(session, party_id, valid_from)
