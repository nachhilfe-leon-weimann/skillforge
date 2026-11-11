from fastapi import APIRouter, status

from app.api.v1.common import DBSession, error_responses
from app.core.auth import Scope, require_scopes
from app.services.billing import payers as payers_service
from app.services.billing.errors import LessonNotFoundError, SevdeskContactAlreadyLinkedError
from app.services.crm.errors import PartyNotFoundError

from .params import LessonId, PartyId, StudentId
from .schemas import (
    LessonPayerResponse,
    ManualBillingPutRequest,
    ManualBillingResponse,
    SevdeskContactPutRequest,
    SevdeskContactResponse,
    StudentPayerResponse,
)

router = APIRouter()


@router.get(
    "/students/{party_id}/payer",
    dependencies=[require_scopes(Scope.BILLING_READ)],
    responses=error_responses(PartyNotFoundError),
)
async def get_student_payer(party_id: StudentId, session: DBSession) -> StudentPayerResponse:
    """Read who pays for a student now, whether that payer has a sevDesk contact, and whether it is billed by hand.

    A lesson that becomes billable stores this payer; lessons that are billable already keep theirs.
    """
    return StudentPayerResponse.from_model(await payers_service.get_student_payer(session, party_id))


@router.post(
    "/lessons/{lesson_id}/resolve-payer",
    dependencies=[require_scopes(Scope.BILLING_WRITE)],
    responses=error_responses(LessonNotFoundError),
)
async def resolve_lesson_payer(lesson_id: LessonId, session: DBSession) -> LessonPayerResponse:
    """Resolve the payer of a billable lesson afresh and store it, replacing the payer it got when it became billable.

    A lesson that is not billable has no payer and stays as it is.
    """
    return LessonPayerResponse.from_model(await payers_service.resolve_lesson_payer(session, lesson_id))


@router.put(
    "/parties/{party_id}/sevdesk-contact",
    dependencies=[require_scopes(Scope.BILLING_WRITE)],
    responses=error_responses(PartyNotFoundError, SevdeskContactAlreadyLinkedError),
)
async def put_sevdesk_contact(
    party_id: PartyId, request: SevdeskContactPutRequest, session: DBSession
) -> SevdeskContactResponse:
    """Link the party to its sevDesk contact, the recipient of its invoices. Idempotent.

    A party has one contact and a contact one party: another link is removed first.
    """
    contact = await payers_service.link_sevdesk_contact(session, party_id, request.sevdesk_contact_id)
    return SevdeskContactResponse.from_model(contact)


@router.delete(
    "/parties/{party_id}/sevdesk-contact",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_scopes(Scope.BILLING_WRITE)],
    responses=error_responses(PartyNotFoundError),
)
async def delete_sevdesk_contact(party_id: PartyId, session: DBSession) -> None:
    """Remove the party's sevDesk contact link. Idempotent: a party without one answers 204 too."""
    await payers_service.unlink_sevdesk_contact(session, party_id)


@router.put(
    "/parties/{party_id}/manual-billing",
    dependencies=[require_scopes(Scope.BILLING_WRITE)],
    responses=error_responses(PartyNotFoundError),
)
async def put_manual_billing(
    party_id: PartyId, request: ManualBillingPutRequest, session: DBSession
) -> ManualBillingResponse:
    """Mark the party as a payer billed by hand: their lessons never reach a draft. Idempotent."""
    return ManualBillingResponse.from_model(
        await payers_service.put_manual_payer(session, party_id, **request.model_dump())
    )


@router.delete(
    "/parties/{party_id}/manual-billing",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_scopes(Scope.BILLING_WRITE)],
    responses=error_responses(PartyNotFoundError),
)
async def delete_manual_billing(party_id: PartyId, session: DBSession) -> None:
    """Unmark the party; its lessons are billed by invoice again. Idempotent: an unmarked party answers 204 too."""
    await payers_service.delete_manual_payer(session, party_id)
