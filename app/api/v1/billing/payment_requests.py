from fastapi import APIRouter

from app.api.v1.common import error_responses
from app.core.auth import Scope, require_scopes
from app.services.billing import payment_requests as payment_requests_service
from app.services.billing.errors import InvoiceNotPayableError, SevdeskRefusedError, SevdeskUnavailableError

from .params import BillingConfig, InvoiceNumber, Sevdesk
from .schemas import PaymentRequest

router = APIRouter(prefix="/payment-requests")


@router.get(
    "/{invoice_number}",
    dependencies=[require_scopes(Scope.BILLING_PAYMENT_READ)],
    responses=error_responses(InvoiceNotPayableError, SevdeskUnavailableError, SevdeskRefusedError),
)
async def get_payment_request(
    invoice_number: InvoiceNumber, sevdesk: Sevdesk, settings: BillingConfig
) -> PaymentRequest:
    """Read the payment state of one invoice from sevDesk: `open` with the amount left to pay, or `paid`, and whether
    PayPal is allowed. Writes nothing.

    A number without a payable invoice - none, a draft, a cancellation, a cancelled original, another document such as
    a dunning letter - is `invoice_not_payable`, which never says which; a draft under the number of an issued
    invoice is set aside. A sevDesk that cannot be read, or answers anything else - several invoices under one number
    that are no draft, an unknown status - is `sevdesk_unavailable`; a configuration that is refused is
    `sevdesk_refused`.
    """
    answer = await payment_requests_service.look_up_payment_request(
        sevdesk, invoice_number, paypal_blocked=settings.paypal_blocked_customer_numbers
    )
    return PaymentRequest.from_model(answer)
