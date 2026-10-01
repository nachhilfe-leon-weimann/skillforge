from fastapi import APIRouter, status

from app.api.v1.common import DBSession, error_responses
from app.core.auth import Scope, require_scopes
from app.services.crm import roles as roles_service
from app.services.crm.errors import PersonNotFoundError, RoleNotFoundError, UnknownSubjectError

from .params import PartyId
from .schemas import PersonDetail, StudentRoleRequest, TutorRoleRequest

router = APIRouter(prefix="/persons/{party_id}")


@router.put(
    "/student",
    dependencies=[require_scopes(Scope.CRM_WRITE)],
    responses=error_responses(PersonNotFoundError, UnknownSubjectError),
)
async def put_student_role(party_id: PartyId, request: StudentRoleRequest, session: DBSession) -> PersonDetail:
    """Give a person the student role, or replace its data. Idempotent; `subject_ids` replaces the whole set."""
    party = await roles_service.put_student_role(session, party_id, **request.model_dump())
    return PersonDetail.from_model(party)


@router.delete(
    "/student",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_scopes(Scope.CRM_WRITE)],
    responses=error_responses(PersonNotFoundError, RoleNotFoundError),
)
async def remove_student_role(party_id: PartyId, session: DBSession) -> None:
    """Take the student role away, and with it every `tutor_of` pointing to the person.

    The tutors on the other side of those relations change too: their `updated_at` moves. Other relations stay.
    """
    await roles_service.remove_student_role(session, party_id)


@router.put(
    "/tutor",
    dependencies=[require_scopes(Scope.CRM_WRITE)],
    responses=error_responses(PersonNotFoundError, UnknownSubjectError),
)
async def put_tutor_role(party_id: PartyId, request: TutorRoleRequest, session: DBSession) -> PersonDetail:
    """Give a person the tutor role, or replace its data. Idempotent; `subject_ids` replaces the whole set."""
    party = await roles_service.put_tutor_role(session, party_id, **request.model_dump())
    return PersonDetail.from_model(party)


@router.delete(
    "/tutor",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_scopes(Scope.CRM_WRITE)],
    responses=error_responses(PersonNotFoundError, RoleNotFoundError),
)
async def remove_tutor_role(party_id: PartyId, session: DBSession) -> None:
    """Take the tutor role away, and with it every `tutor_of` starting at the person.

    The students on the other side of those relations change too: their `updated_at` moves. Other relations stay.
    """
    await roles_service.remove_tutor_role(session, party_id)
