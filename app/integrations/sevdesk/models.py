"""sevDesk's objects as SkillForge reads them.

sevDesk types numbers and booleans as strings (``"12.50"``, ``"0"``/``"1"``): the models accept them through
pydantic's lax mode but hold strict types. They declare only the fields SkillForge uses and ignore the rest,
so a person's name, address or bank data is never read where it is not needed; a declared name stays out of
``repr``. An amount (``Money``) holds whole cents: a sub-cent value is refused, never rounded.
"""

from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field
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


CENT = Decimal("0.01")


def _whole_cents(amount: Decimal) -> Decimal:
    cents = amount.quantize(CENT)
    if cents != amount:
        raise ValueError("an amount below a cent")
    return cents


type Money = Annotated[Decimal, AfterValidator(_whole_cents)]


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


class ContactCustomField(SevdeskObject):
    contact: ObjectRef
    contact_custom_field_setting: SingleRef
    value: str = Field(repr=False)


class Part(SevdeskObject):
    name: str
    part_number: str | None = None
    unity: ObjectRef | None = None
    price: Money | None = None
    tax_rate: Decimal | None = None
    status: int | None = None
