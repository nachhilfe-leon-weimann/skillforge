from datetime import UTC, datetime

from fastapi import APIRouter, Response, status

from app.api.v1.common import DBSession, Page, PageQuery, error_responses
from app.core.auth import Scope, require_scopes
from app.services.billing import calendar as calendar_service
from app.services.billing import calendar_aliases as aliases_service
from app.services.billing.errors import (
    CalendarAliasAlreadyExistsError,
    CalendarAliasNotFoundError,
    CalendarEventAlreadyAssignedError,
    CalendarEventNotAssignableError,
    CalendarEventNotFoundError,
    CalendarUnavailableError,
    LessonAlreadyExistsError,
    UnknownStudentError,
)

from .params import LESSON_LOCATION, AliasId, CalendarSource, EventId
from .schemas import (
    CalendarAliasCreateRequest,
    CalendarAliasResponse,
    CalendarEventAssignRequest,
    CalendarEventResponse,
    CalendarImportRequest,
    CalendarImportResponse,
    LessonResponse,
)

router = APIRouter(prefix="/calendar")


@router.post(
    "/import",
    dependencies=[require_scopes(Scope.BILLING_WRITE)],
    responses=error_responses(CalendarUnavailableError),
)
async def import_calendar(
    request: CalendarImportRequest, reader: CalendarSource, session: DBSession
) -> CalendarImportResponse:
    """Import the lesson calendar for a range of Berlin days: the calendar proposes, a person confirms.

    A new occurrence becomes a `planned` lesson, a `cancelled` one for a late cancellation, or none: a phone call, an
    all-day entry or a first meeting is kept with its reason and not listed; any other occurrence without a lesson
    waits for an assignment. An untouched lesson - `planned`, no note, no own rate, at the start and length of its
    occurrence as last seen - goes when its occurrence vanishes, and follows a moved, resized or cancelled appointment
    only while the title rules still give this student and this subject. Otherwise the lesson never changes and its
    occurrence is flagged `changed`. Importing an unchanged
    range again changes nothing. Answers with counts only; a calendar that cannot be read is `calendar_unavailable`,
    and nothing is imported.
    """
    counts = await calendar_service.import_calendar(
        session, reader, first_day=request.first_day, last_day=request.last_day, now=datetime.now(UTC)
    )
    return CalendarImportResponse.from_model(counts)


@router.get("/events", dependencies=[require_scopes(Scope.BILLING_READ)])
async def list_calendar_events(params: PageQuery, session: DBSession) -> Page[CalendarEventResponse]:
    """List the occurrences that wait for an assignment or are flagged `changed`, by start."""
    events, total = await calendar_service.list_events(session, limit=params.limit, offset=params.offset)
    return Page.of([CalendarEventResponse.from_model(event) for event in events], total=total, params=params)


@router.post(
    "/events/{event_id}/assign",
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_scopes(Scope.BILLING_WRITE)],
    responses=error_responses(
        CalendarEventNotFoundError,
        UnknownStudentError,
        CalendarEventNotAssignableError,
        CalendarEventAlreadyAssignedError,
        LessonAlreadyExistsError,
        CalendarAliasAlreadyExistsError,
    ),
)
async def assign_calendar_event(
    event_id: EventId, request: CalendarEventAssignRequest, response: Response, session: DBSession
) -> LessonResponse:
    """Create the lesson of an occurrence without one - `cancelled` for a cancelled occurrence - and optionally store
    its name as the student's alias. The lesson starts at the occurrence's start: one off the whole minute cannot be
    assigned. The `Location` header points to the lesson."""
    lesson = await calendar_service.assign_event(session, event_id, **request.model_dump())
    response.headers["Location"] = LESSON_LOCATION.format(lesson_id=lesson.id)
    return LessonResponse.from_model(lesson)


@router.post(
    "/events/{event_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_scopes(Scope.BILLING_WRITE)],
    responses=error_responses(CalendarEventNotFoundError),
)
async def dismiss_calendar_event(event_id: EventId, session: DBSession) -> None:
    """Take an occurrence off the list. Without a lesson it waits for no assignment and is not proposed again. With
    one, the occurrence as last seen is acknowledged: a "no lesson" answer of the rules and a change already seen stay
    quiet until it changes again. A title naming another student or subject is never acknowledged - the next import
    flags it again. Idempotent."""
    await calendar_service.dismiss_event(session, event_id)


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
