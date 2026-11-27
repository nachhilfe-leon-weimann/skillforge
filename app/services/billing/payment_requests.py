"""The payment-request lookup (billing spec, "Payment-request lookup"): the payment state of one invoice by its number.

The payment page asks before it sends a family to PayPal. After the identity check the lookup reads the invoices
sevDesk lists under the number and, for an answer, the invoice's contact for its customer number. It only reads -
neither sevDesk nor the database is written - so it works in ``dry_run``.

- ``payment_state`` holds the answer table: an ``RE`` invoice sent (200) or partly paid (750) is ``open`` with
  ``sumGross`` minus ``paidAmount`` left, in ``int`` cents through ``Money``; one paid (1000 with a paid amount) is
  ``paid``. No invoice, a draft, another type and a cancelled original (1000, nothing paid; Q12) are
  ``InvoiceNotPayableError``, which never says which. Drafts are set aside before that, so a draft that kept an issued
  invoice's number (Q3) shadows nothing. Anything else - several invoices under the number that are no draft
  included - is ``SevdeskUnavailableError``: the page then fails closed.
- ``paypal_permission``: ``blocked`` exactly for a contact whose customer number is in
  ``BILLING__PAYPAL_BLOCKED_CUSTOMER_NUMBERS``.
- Every integration error is translated here ("Errors from an integration"). No answer, message or log line holds a
  name, a contact id, a customer number or the setting.
"""

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum

from app.core.logging import get_logger
from app.integrations.errors import IntegrationError, IntegrationRefusedError
from app.integrations.sevdesk.client import SevdeskClient
from app.integrations.sevdesk.config import SevdeskSettings
from app.integrations.sevdesk.models import Contact, Invoice
from app.integrations.transport import InnerTransport

from .errors import InvoiceNotPayableError, SevdeskRefusedError, SevdeskUnavailableError

INVOICE_NUMBER_PATTERN = r"^[A-Z0-9][A-Z0-9._-]{0,31}$"
"""What an invoice number is to the lookup: one path segment, no ``/``; sevDesk's read ``RE-%NUMBER`` (Q3)."""

INVOICE_TYPE = "RE"
"""The only type that is ever ``open`` or ``paid``; a cancellation is ``SR``, a dunning letter another type."""
DRAFT = 100
OPEN_STATUSES = frozenset({200, 750})
"""Sent, and partly paid (Q13)."""
PAID = 1000

type SevdeskConnect = Callable[[], AbstractAsyncContextManager[SevdeskClient]]
"""A sevDesk client whose account passed the identity check, for one block; raises an ``IntegrationError``."""

logger = get_logger(__name__)


class PaymentState(StrEnum):
    OPEN = "open"
    PAID = "paid"


class PayPalPermission(StrEnum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class PaymentRequestState:
    """The answer of the lookup: four fields, nothing about the person."""

    invoice_number: str
    state: PaymentState
    open_amount_cents: int
    paypal: PayPalPermission


def sevdesk_connect(settings: SevdeskSettings, *, transport: InnerTransport | None = None) -> SevdeskConnect:
    """sevDesk from the settings: each lookup connects anew, so the account is confirmed every time."""
    return lambda: SevdeskClient.connect(settings, transport=transport)


async def look_up_payment_request(
    connect: SevdeskConnect, invoice_number: str, *, paypal_blocked: frozenset[str]
) -> PaymentRequestState:
    """The payment state of the invoice ``invoice_number``; ``paypal_blocked`` holds the blocked customer numbers."""
    try:
        async with connect() as client:
            listed = await client.invoices_by_number(invoice_number)
            invoice, state, open_amount_cents = payment_state(invoice_number, listed)
            contact = await client.get_contact(invoice.contact.id)
    except IntegrationRefusedError as exc:  # the identity check's IdentityCheckError included
        # No chaining and no text of the cause: it may carry account ids or the remote system's message.
        logger.warning("payment_request_refused", error=type(exc).__name__, status=exc.status)
        raise SevdeskRefusedError(f"The sevDesk call was refused ({type(exc).__name__})") from None
    except IntegrationError as exc:
        logger.warning("payment_request_unavailable", error=type(exc).__name__, status=exc.status)
        raise SevdeskUnavailableError(f"The sevDesk read failed ({type(exc).__name__})") from None
    if contact is None:
        raise _unexpected("the invoice's contact is unknown")
    return PaymentRequestState(
        invoice_number=invoice_number,
        state=state,
        open_amount_cents=open_amount_cents,
        paypal=paypal_permission(contact, paypal_blocked),
    )


def payment_state(invoice_number: str, listed: Sequence[Invoice]) -> tuple[Invoice, PaymentState, int]:
    """The answer table: the invoice under ``invoice_number``, its state and its open amount in cents.

    Only an invoice that carries exactly this number counts; how sevDesk filters is Q19's, so a listed invoice under
    another number is none of it. Drafts are set aside first: a draft is never payable, and it can keep a number an
    issued invoice took at issue (Q3), so only several invoices that are no draft are ambiguous.
    """
    matches = [invoice for invoice in listed if invoice.invoice_number == invoice_number]
    if not matches:
        raise InvoiceNotPayableError("No invoice carries the number")
    issued = [invoice for invoice in matches if invoice.status != DRAFT]
    if not issued:
        raise InvoiceNotPayableError("Only drafts carry the number")
    if len(issued) > 1:
        raise _unexpected("several invoices that are no draft carry the number")
    [invoice] = issued
    if invoice.invoice_type is None:
        raise _unexpected("the invoice has no type")
    if invoice.invoice_type != INVOICE_TYPE:
        raise InvoiceNotPayableError("The document is no invoice")
    gross, paid = invoice.sum_gross_cents, invoice.paid_amount_cents
    if gross is None or paid is None:
        raise _unexpected("the invoice lacks its gross sum or its paid amount")
    if paid < 0:
        raise _unexpected("the paid amount is below 0")
    if invoice.status in OPEN_STATUSES:
        open_amount_cents = gross - paid
        if open_amount_cents < 0:
            raise _unexpected("the open amount is below 0")
        return invoice, PaymentState.OPEN, open_amount_cents
    if invoice.status == PAID:
        if paid == 0:
            raise InvoiceNotPayableError("The invoice is a cancelled original")
        return invoice, PaymentState.PAID, 0
    raise _unexpected(f"the invoice has status {invoice.status}")


def paypal_permission(contact: Contact, blocked: frozenset[str]) -> PayPalPermission:
    """``blocked`` exactly for a contact whose customer number is one of ``blocked``; a contact without one is
    ``allowed``."""
    if contact.customer_number is not None and contact.customer_number in blocked:
        return PayPalPermission.BLOCKED
    return PayPalPermission.ALLOWED


def _unexpected(case: str) -> SevdeskUnavailableError:
    """An answer the table does not name: sevDesk is treated as unreadable, so the page fails closed."""
    logger.warning("payment_request_unexpected", case=case)
    return SevdeskUnavailableError(f"sevDesk answered what the lookup does not name: {case}")
