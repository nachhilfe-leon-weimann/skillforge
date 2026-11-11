from fastapi import APIRouter, Response, status

from app.api.v1.common import DBSession, Page, error_responses
from app.core.auth import Scope, require_scopes
from app.services.billing import lessons as lessons_service
from app.services.billing.errors import (
    LessonAlreadyExistsError,
    LessonNotBillableError,
    LessonNotFoundError,
    RateNotQuarterHourError,
    UnknownStudentError,
)

from .params import LESSON_LOCATION, LessonId, LessonListQuery
from .schemas import LessonCreateRequest, LessonResponse, LessonUpdateRequest

# There is no delete route (money rule 14): "remove" is the status `not_held`.
router = APIRouter(prefix="/lessons")


@router.get("", dependencies=[require_scopes(Scope.BILLING_READ)])
async def list_lessons(params: LessonListQuery, session: DBSession) -> Page[LessonResponse]:
    """List lessons by start. `from` and `to` are days in `Europe/Berlin`, both included."""
    lessons, total = await lessons_service.list_lessons(
        session,
        limit=params.limit,
        offset=params.offset,
        from_day=params.from_,
        to_day=params.to,
        student_id=params.student_id,
        status=params.status,
    )
    return Page.of([LessonResponse.from_model(lesson) for lesson in lessons], total=total, params=params)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_scopes(Scope.BILLING_WRITE)],
    responses=error_responses(UnknownStudentError, RateNotQuarterHourError, LessonAlreadyExistsError),
)
async def create_lesson(request: LessonCreateRequest, response: Response, session: DBSession) -> LessonResponse:
    """Add a lesson by hand. It starts `planned`; tick it with `PATCH`. The `Location` header points to it."""
    lesson = await lessons_service.create_lesson(session, **request.model_dump())
    response.headers["Location"] = LESSON_LOCATION.format(lesson_id=lesson.id)
    return LessonResponse.from_model(lesson)


@router.get(
    "/{lesson_id}",
    dependencies=[require_scopes(Scope.BILLING_READ)],
    responses=error_responses(LessonNotFoundError),
)
async def get_lesson(lesson_id: LessonId, session: DBSession) -> LessonResponse:
    """Read a lesson."""
    return LessonResponse.from_model(await lessons_service.get_lesson(session, lesson_id))


@router.patch(
    "/{lesson_id}",
    dependencies=[require_scopes(Scope.BILLING_WRITE)],
    responses=error_responses(
        LessonNotFoundError,
        UnknownStudentError,
        RateNotQuarterHourError,
        LessonNotBillableError,
        LessonAlreadyExistsError,
    ),
)
async def update_lesson(lesson_id: LessonId, request: LessonUpdateRequest, session: DBSession) -> LessonResponse:
    """Change a lesson: tick it (`status: held`), switch `billable`, or correct its data.

    Only the fields that are sent change; an empty body changes nothing.
    """
    lesson = await lessons_service.update_lesson(session, lesson_id, **request.model_dump())
    return LessonResponse.from_model(lesson)
