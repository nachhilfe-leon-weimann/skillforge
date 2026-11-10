"""The draft invoice SkillForge hands to sevDesk, and the ``saveInvoice`` body built from it.

Texts and amounts come from the caller; this module knows only sevDesk's wire format. On bookkeeping
System 2.0 ``saveInvoice`` creates drafts only, the VAT regulation is a ``taxRule`` (no ``taxType``), and
the body always carries ``invoicePosDelete``, ``discountSave``, ``discountDelete`` and ``takeDefaultAddress``,
in that order. No amount passes through a float: prices are integer cents, quantities integer minutes, and both
go out as exact decimal strings. The quarter-hour rule (money rule 3) holds by construction: minutes come in steps
of 15 and a price per hour is divisible by 4, so every position is whole cents and nothing is ever rounded.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, Inexact, localcontext

DRAFT_STATUS = 100
INVOICE_TYPE = "RE"
CURRENCY = "EUR"
SMALL_BUSINESS_TAX_RULE = 11
"""System 2.0 tax rule for a small business under § 19 UStG: no VAT on any position."""
MINUTES_PER_HOUR = 60
MINUTE_STEP = 15
"""A position's minutes come in quarter hours."""
CENTS_PER_HOUR_STEP = 4
"""A price per hour is divisible by 4, so a quarter hour costs whole cents."""


@dataclass(frozen=True, slots=True)
class PositionDraft:
    """One position counted in hours: ``minutes`` of tutoring at ``price_cents`` per hour."""

    name: str
    minutes: int
    price_cents: int
    """Price per hour in cents; under § 19 UStG net and gross are the same."""
    unity_id: int
    """The hour unit (``SEVDESK__HOUR_UNIT_ID``)."""
    part_id: int | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        for name in ("minutes", "price_cents", "unity_id", "part_id"):
            value = getattr(self, name)
            if not (type(value) is int or (name == "part_id" and value is None)):
                raise TypeError(f"a position's {name} must be an int, not {type(value).__name__}")
        if self.minutes <= 0 or self.price_cents < 0:
            raise ValueError("a position needs positive minutes and a price of at least zero")
        if self.minutes % MINUTE_STEP or self.price_cents % CENTS_PER_HOUR_STEP:
            raise ValueError(
                f"a position needs minutes in steps of {MINUTE_STEP} and cents per hour divisible by "
                f"{CENTS_PER_HOUR_STEP} (the quarter-hour rule)"
            )


@dataclass(frozen=True, slots=True)
class InvoiceDraft:
    contact_id: int
    invoice_date: date
    delivery_date: date
    """The day of the service, or with ``delivery_date_until`` the first day of the period."""
    header: str
    tax_text: str
    positions: tuple[PositionDraft, ...]
    delivery_date_until: date | None = None
    customer_internal_note: str | None = None
    """Not shown to the customer; carries the reference a reconcile searches for."""
    head_text: str | None = None
    foot_text: str | None = None
    time_to_pay: int | None = None

    def __post_init__(self) -> None:
        if self.delivery_date_until is not None and self.delivery_date_until < self.delivery_date:
            raise ValueError("the delivery period ends before it starts")


def hours(minutes: int) -> Decimal:
    """``minutes`` in hours, exactly; a ``ValueError`` for a count with no finite decimal (50 minutes)."""
    with localcontext() as context:
        context.traps[Inexact] = True
        try:
            return Decimal(minutes) / MINUTES_PER_HOUR
        except Inexact:
            raise ValueError(f"{minutes} minutes are no exact decimal number of hours") from None


def euros(cents: int) -> str:
    """Integer cents as sevDesk's decimal string, exact at any size: ``3000`` is ``"30.00"``."""
    if type(cents) is not int:
        raise TypeError(f"cents must be an int, not {type(cents).__name__}")
    whole, rest = divmod(abs(cents), 100)
    return f"{'-' if cents < 0 else ''}{whole}.{rest:02d}"


def invoice_payload(draft: InvoiceDraft, *, contact_person_id: int) -> dict[str, object]:
    """The ``POST /Invoice/Factory/saveInvoice`` body for a draft under § 19 UStG."""
    invoice = {
        "objectName": "Invoice",
        "mapAll": True,
        "invoiceDate": draft.invoice_date.isoformat(),
        "deliveryDate": draft.delivery_date.isoformat(),
        "deliveryDateUntil": draft.delivery_date_until and draft.delivery_date_until.isoformat(),
        "contact": _ref("Contact", draft.contact_id),
        "contactPerson": _ref("SevUser", contact_person_id),
        "status": DRAFT_STATUS,
        "invoiceType": INVOICE_TYPE,
        "currency": CURRENCY,
        "taxRule": _ref("TaxRule", SMALL_BUSINESS_TAX_RULE),
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
        "quantity": f"{hours(position.minutes):f}",
        "price": euros(position.price_cents),
        "taxRate": "0",
        "unity": _ref("Unity", position.unity_id),
        "part": None if position.part_id is None else _ref("Part", position.part_id),
    }
    return {key: value for key, value in fields.items() if value is not None}


def _ref(object_name: str, object_id: int) -> dict[str, object]:
    return {"id": object_id, "objectName": object_name}
