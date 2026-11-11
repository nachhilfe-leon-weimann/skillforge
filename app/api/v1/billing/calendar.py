from fastapi import APIRouter, status

from app.api.v1.common import DBSession, Page, PageQuery, error_responses
from app.core.auth import Scope, require_scopes
from app.services.billing import calendar_aliases as aliases_service
from app.services.billing.errors import (
    CalendarAliasAlreadyExistsError,
    CalendarAliasNotFoundError,
    UnknownStudentError,
)

from .params import AliasId
from .schemas import CalendarAliasCreateRequest, CalendarAliasResponse

router = APIRouter(prefix="/calendar")


@router.get("/aliases", dependencies=[require_scopes(Scope.BILLING_READ)])
async def list_calendar_aliases(params: PageQuery, session: DBSession) -> Page[CalendarAliasResponse]:
    """List the calendar aliases by their normalised name."""
    aliases, total = await aliases_service.list_aliases(session, limit=params.limit, offset=params.offset)
    return Page.of([CalendarAliasResponse.from_model(alias) for alias in aliases], total=total, params=params)


@router.post(
    "/aliases",
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_scopes(Scope.BILLING_WRITE)],
    responses=error_responses(UnknownStudentError, CalendarAliasAlreadyExistsError),
)
async def create_calendar_alias(request: CalendarAliasCreateRequest, session: DBSession) -> CalendarAliasResponse:
    """Map a name as written in the lesson calendar to a student. It wins over matching by the students' names.

    The name is stored normalised; a name that normalises like a stored one is `calendar_alias_already_exists`.
    """
    alias = await aliases_service.create_alias(session, **request.model_dump())
    return CalendarAliasResponse.from_model(alias)


@router.delete(
    "/aliases/{alias_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_scopes(Scope.BILLING_WRITE)],
    responses=error_responses(CalendarAliasNotFoundError),
)
async def delete_calendar_alias(alias_id: AliasId, session: DBSession) -> None:
    """Remove a calendar alias; the name is then matched by the students' names again."""
    await aliases_service.delete_alias(session, alias_id)
