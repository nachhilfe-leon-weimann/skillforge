"""sevDesk's objects as SkillForge reads them.

sevDesk types numbers and booleans as strings (``"12.50"``, ``"0"``/``"1"``): the models accept them through
pydantic's lax mode but hold strict types. They declare only the fields SkillForge uses and ignore the rest,
so a person's name, address or bank data is never read where it is not needed; a declared name stays out of
``repr``. An amount (``Money``) is ``int`` cents, read from sevDesk's euros through ``Decimal`` once: a sub-cent
value and a ``float`` are refused, never rounded.
"""

from datetime import datetime
from decimal import Decimal, DecimalException, Inexact, localcontext
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictInt
from pydantic.alias_generators import to_camel


class SevdeskModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True, extra="ignore", frozen=True)


class SevdeskObject(SevdeskModel):
    """An object with its own id."""

    id: int


class ObjectRef(SevdeskModel):
    """A reference to another object: ``{"id": "7", "objectName": "Contact"}``."""

    id: int
    object_name: str


def unwrap_single(value: object) -> object:
    """sevDesk's spec types some single objects as a list of one; accept that form too."""
    return value[0] if isinstance(value, list) and len(value) == 1 else value


type SingleRef = Annotated[ObjectRef, BeforeValidator(unwrap_single)]


def _cents(euros: object) -> int:
    """An amount as sevDesk sends it - euros as a string, an integer or a ``Decimal`` (``Reply.json``) - in cents."""
    if isinstance(euros, float):
        raise ValueError("an amount must not pass through float")
    if isinstance(euros, bool) or not isinstance(euros, str | int | Decimal):
        raise ValueError("an amount must be a decimal string or a number")
    try:
        # Exact or refused: an amount longer than the context's precision traps instead of being rounded.
        with localcontext() as context:
            context.traps[Inexact] = True
            cents = Decimal(euros).scaleb(2)
    except DecimalException:
        raise ValueError("an amount must be an exact decimal number") from None
    if not cents.is_finite():
        raise ValueError("an amount must be an exact decimal number")
    if cents != cents.to_integral_value():
        raise ValueError("an amount below a cent")
    return int(cents)


type Money = Annotated[StrictInt, BeforeValidator(_cents)]
"""An amount in ``int`` cents, parsed exactly from the euros sevDesk sends; a read type, never fed cents."""


class BookkeepingVersion(SevdeskModel):
    version: str


class Account(SevdeskObject):
    """The ``SevClient``: the sevDesk account a token belongs to."""

    name: str = Field(repr=False)


class SevUser(SevdeskObject):
    """A user of the account; only its id and account are read, never its name."""

    sev_client: ObjectRef | None = None


class Unity(SevdeskObject):
    """A unit of quantity, such as hours."""

    name: str


class Contact(SevdeskObject):
    customer_number: str | None = None
    category: ObjectRef | None = None
    sev_client: ObjectRef | None = None


class CustomFieldSetting(SevdeskObject):
    """A ``ContactCustomFieldSetting``: the definition of a custom contact field."""

    name: str
    identifier: str | None = None
    sev_client: ObjectRef | None = None


class ContactCustomField(SevdeskObject):
    contact: ObjectRef
    contact_custom_field_setting: SingleRef
    value: str = Field(repr=False)


class Part(SevdeskObject):
    name: str
    part_number: str | None = None
    unity: ObjectRef | None = None
    price_cents: Money | None = Field(default=None, alias="price")
    tax_rate: Decimal | None = None
    status: int | None = None


class InvoiceSequence(SevdeskObject):
    """The ``SevSequence`` that numbers invoices: its format and the next number."""

    format: str
    next_sequence: int
    type: str


class Invoice(SevdeskObject):
    invoice_number: str | None = None
    status: int
    contact: ObjectRef
    contact_person: ObjectRef | None = None
    invoice_date: datetime | None = None
    tax_rule: ObjectRef | None = None
    small_settlement: bool | None = None
    sum_net_cents: Money | None = Field(default=None, alias="sumNet")
    sum_gross_cents: Money | None = Field(default=None, alias="sumGross")
    customer_internal_note: str | None = None
    foot_text: str | None = Field(default=None, repr=False)
    sev_client: ObjectRef | None = None


class InvoicePosition(SevdeskObject):
    invoice: ObjectRef
    part: ObjectRef | None = None
    unity: ObjectRef | None = None
    quantity: Decimal
    price_cents: Money | None = Field(default=None, alias="price")
    tax_rate: Decimal | None = None
    position_number: int | None = None
    name: str | None = Field(default=None, repr=False)


class SavedInvoice(SevdeskModel):
    """What ``saveInvoice`` answers: the invoice and its positions."""

    invoice: Invoice
    positions: list[InvoicePosition] = Field(default_factory=list, alias="invoicePos")
