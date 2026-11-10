"""sevDesk's objects as SkillForge reads them.

sevDesk types numbers and booleans as strings (``"12.50"``, ``"0"``/``"1"``): the models accept them through
pydantic's lax mode but hold strict types. They declare only the fields SkillForge uses and ignore the rest,
so a person's name, address or bank data is never read where it is not needed; a declared name stays out of
``repr``.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
from pydantic.alias_generators import to_camel


class SevdeskModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True, extra="ignore", frozen=True)


class ObjectRef(SevdeskModel):
    """A reference to another object: ``{"id": "7", "objectName": "Contact"}``."""

    id: int
    object_name: str


def unwrap_single(value: object) -> object:
    """sevDesk's spec types some single objects as a list of one; accept that form too."""
    return value[0] if isinstance(value, list) and len(value) == 1 else value


type SingleRef = Annotated[ObjectRef, BeforeValidator(unwrap_single)]


class BookkeepingVersion(SevdeskModel):
    version: str


class Account(SevdeskModel):
    """The ``SevClient``: the sevDesk account a token belongs to."""

    id: int
    name: str = Field(repr=False)


class SevUser(SevdeskModel):
    """A user of the account; only its id and account are read, never its name."""

    id: int
    sev_client: ObjectRef | None = None


class Unity(SevdeskModel):
    """A unit of quantity, such as hours."""

    id: int
    name: str


class Contact(SevdeskModel):
    id: int
    customer_number: str | None = None
    category: ObjectRef | None = None
    sev_client: ObjectRef | None = None


class CustomFieldSetting(SevdeskModel):
    """A ``ContactCustomFieldSetting``: the definition of a custom contact field."""

    id: int
    name: str
    identifier: str | None = None


class ContactCustomField(SevdeskModel):
    id: int
    contact: ObjectRef
    contact_custom_field_setting: SingleRef
    value: str = Field(repr=False)


class Part(SevdeskModel):
    id: int
    name: str
    part_number: str | None = None
    unity: ObjectRef | None = None
    price: Decimal | None = None
    tax_rate: Decimal | None = None
    status: int | None = None


class InvoiceSequence(SevdeskModel):
    """The ``SevSequence`` that numbers invoices: its format and the next number."""

    id: int
    format: str
    next_sequence: int
    type: str


class Invoice(SevdeskModel):
    id: int
    invoice_number: str | None = None
    status: int
    contact: ObjectRef
    contact_person: ObjectRef | None = None
    invoice_date: datetime | None = None
    tax_rule: ObjectRef | None = None
    small_settlement: bool | None = None
    sum_net: Decimal | None = None
    sum_gross: Decimal | None = None
    customer_internal_note: str | None = None
    foot_text: str | None = Field(default=None, repr=False)
    sev_client: ObjectRef | None = None


class InvoicePosition(SevdeskModel):
    id: int
    invoice: ObjectRef
    part: ObjectRef | None = None
    unity: ObjectRef | None = None
    quantity: Decimal
    price: Decimal | None = None
    tax_rate: Decimal | None = None
    position_number: int | None = None
    name: str | None = Field(default=None, repr=False)


class SavedInvoice(SevdeskModel):
    """What ``saveInvoice`` answers: the invoice and its positions."""

    invoice: Invoice
    positions: list[InvoicePosition] = Field(default_factory=list, alias="invoicePos")
