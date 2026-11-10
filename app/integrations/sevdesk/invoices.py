"""The draft invoice SkillForge hands to sevDesk, and the ``saveInvoice`` body built from it.

Texts and amounts come from the caller; this module knows only sevDesk's wire format. On bookkeeping
System 2.0 ``saveInvoice`` creates drafts only, the VAT regulation is a ``taxRule`` (no ``taxType``), and
the body always carries ``invoicePosDelete``, ``discountSave``, ``discountDelete`` and ``takeDefaultAddress``,
in that order.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

DRAFT_STATUS = 100
INVOICE_TYPE = "RE"
CURRENCY = "EUR"
SMALL_BUSINESS_TAX_RULE = 11
"""System 2.0 tax rule for a small business under § 19 UStG: no VAT on any position."""


@dataclass(frozen=True, slots=True)
class PositionDraft:
    name: str
    quantity: Decimal
    price: Decimal
    """Price per unit; under § 19 UStG net and gross are the same."""
    unity_id: int
    part_id: int | None = None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class InvoiceDraft:
    contact_id: int
    invoice_date: date
    header: str
    tax_text: str
    positions: tuple[PositionDraft, ...]
    customer_internal_note: str | None = None
    """Not shown to the customer; carries the reference a reconcile searches for."""
    head_text: str | None = None
    foot_text: str | None = None
    time_to_pay: int | None = None


def invoice_payload(draft: InvoiceDraft, *, contact_person_id: int) -> dict[str, object]:
    """The ``POST /Invoice/Factory/saveInvoice`` body for a draft under § 19 UStG."""
    invoice = {
        "objectName": "Invoice",
        "mapAll": True,
        "invoiceDate": draft.invoice_date.isoformat(),
        "contact": _ref("Contact", draft.contact_id),
        "contactPerson": _ref("SevUser", contact_person_id),
        "status": DRAFT_STATUS,
        "invoiceType": INVOICE_TYPE,
        "currency": CURRENCY,
        "taxRule": {"id": str(SMALL_BUSINESS_TAX_RULE), "objectName": "TaxRule"},
        "taxText": draft.tax_text,
        "header": draft.header,
        "headText": draft.head_text,
        "footText": draft.foot_text,
        "customerInternalNote": draft.customer_internal_note,
        "timeToPay": draft.time_to_pay,
    }
    return {
        "invoice": {key: value for key, value in invoice.items() if value is not None},
        "invoicePosSave": [_position(position) for position in draft.positions],
        "invoicePosDelete": None,
        "discountSave": None,
        "discountDelete": None,
        "takeDefaultAddress": True,
    }


def _position(position: PositionDraft) -> dict[str, object]:
    fields = {
        "objectName": "InvoicePos",
        "mapAll": True,
        "name": position.name,
        "text": position.text,
        # JSON numbers: a quantity in hours and a price in cents round-trip exactly through float.
        "quantity": float(position.quantity),
        "price": float(position.price),
        "taxRate": 0,
        "unity": _ref("Unity", position.unity_id),
        "part": None if position.part_id is None else _ref("Part", position.part_id),
    }
    return {key: value for key, value in fields.items() if value is not None}


def _ref(object_name: str, object_id: int) -> dict[str, object]:
    return {"id": object_id, "objectName": object_name}
