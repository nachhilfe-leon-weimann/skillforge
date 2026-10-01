from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import UUID, Enum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..shared import TimestampMixin
from .base import CoreBase

if TYPE_CHECKING:
    from ..ext.clockodo_customer import ClockodoCustomer
    from ..ext.clockodo_project import ClockodoProject
    from ..ext.microsoft_account import MicrosoftAccount
    from ..ext.microsoft_contact import MicrosoftContact
    from ..ext.sevdesk_contact import SevdeskContact
    from .company import Company
    from .contact_info import ContactInfo
    from .party_relation import PartyRelation
    from .person import Person


class PartyType(enum.StrEnum):
    PERSON = "person"
    COMPANY = "company"


class Party(TimestampMixin, CoreBase):
    __tablename__ = "party"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    type: Mapped[PartyType] = mapped_column(
        Enum(PartyType, name="party_type"),
        nullable=False,
    )

    person: Mapped[Person | None] = relationship(
        "Person",
        back_populates="party",
        uselist=False,
        cascade="all, delete-orphan",
        single_parent=True,
    )

    company: Mapped[Company | None] = relationship(
        "Company",
        back_populates="party",
        uselist=False,
        cascade="all, delete-orphan",
        single_parent=True,
    )

    contact_infos: Mapped[list[ContactInfo]] = relationship(
        "ContactInfo",
        back_populates="party",
        cascade="all, delete-orphan",
    )

    outgoing_relations: Mapped[list[PartyRelation]] = relationship(
        "PartyRelation",
        back_populates="from_party",
        cascade="all, delete-orphan",
        foreign_keys="PartyRelation.from_party_id",
    )

    incoming_relations: Mapped[list[PartyRelation]] = relationship(
        "PartyRelation",
        back_populates="to_party",
        cascade="all, delete-orphan",
        foreign_keys="PartyRelation.to_party_id",
    )

    sevdesk_contact: Mapped[SevdeskContact | None] = relationship(
        "SevdeskContact",
        back_populates="party",
        uselist=False,
        cascade="all, delete-orphan",
        single_parent=True,
    )

    clockodo_customer: Mapped[ClockodoCustomer | None] = relationship(
        "ClockodoCustomer",
        back_populates="party",
        uselist=False,
        cascade="all, delete-orphan",
        single_parent=True,
    )

    clockodo_project: Mapped[ClockodoProject | None] = relationship(
        "ClockodoProject",
        back_populates="party",
        uselist=False,
        cascade="all, delete-orphan",
        single_parent=True,
    )

    microsoft_account: Mapped[MicrosoftAccount | None] = relationship(
        "MicrosoftAccount",
        back_populates="party",
        uselist=False,
        cascade="all, delete-orphan",
        single_parent=True,
    )

    microsoft_contact: Mapped[MicrosoftContact | None] = relationship(
        "MicrosoftContact",
        back_populates="party",
        uselist=False,
        cascade="all, delete-orphan",
        single_parent=True,
    )
