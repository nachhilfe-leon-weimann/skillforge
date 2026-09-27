"""Read and write models of the CRM API.

Read models resolve (titles, display names) and are built by explicit ``from_model`` mappers;
write models take IDs and raw values. Update models use Pydantic's experimental ``MISSING``
sentinel: a field is optional but not nullable, and an unset field is absent from ``model_dump()``.
"""

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Literal, Self, assert_never

from pydantic import AfterValidator, Field, StringConstraints, ValidationInfo, field_validator
from pydantic.experimental.missing_sentinel import MISSING

from app.api.v1.common import ApiModel
from app.core.db.models import (
    ContactInfo,
    ContactInfoType,
    Party,
    PartyRelationType,
    PartyType,
    Person,
    PreferredMeetingTool,
    Student,
    Subject,
    Tutor,
)
from app.services.crm.inputs import (
    MAX_CONTACT_VALUE_LENGTH,
    NewContactInfo,
    PartyRole,
    RelationDirection,
    StudentRoleData,
    TutorRoleData,
    normalize_contact_value,
    require_storable_text,
)
from app.services.crm.relations import PartyRelationView

from .params import MAX_SUBJECT_ID

# A plain assignment inlines the constraints at the field; a PEP 695 alias would become its own schema.
Name = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    AfterValidator(require_storable_text),
]


class SubjectResponse(ApiModel):
    """A subject students learn and tutors teach."""

    id: int
    """ID of the subject."""
    title: str
    """Title of the subject, unique regardless of case."""

    @classmethod
    def from_model(cls, subject: Subject) -> Self:
        return cls(id=subject.id, title=subject.title)


class SubjectCreateRequest(ApiModel):
    """Body of `POST /subjects`."""

    title: Name = Field(examples=["Mathematics"])
    """Title of the subject. Surrounding whitespace is stripped; must be unique regardless of case."""


class SubjectUpdateRequest(ApiModel):
    """Body of `PATCH /subjects/{subject_id}`: only the fields that are sent change."""

    title: Name | MISSING = MISSING
    """New title of the subject. Surrounding whitespace is stripped; must be unique regardless of case."""


# --- Contact infos ---


class ContactInfoResponse(ApiModel):
    """One way to reach a party."""

    id: uuid.UUID
    """ID of the contact info."""
    type: ContactInfoType
    """Kind of the contact info; it never changes."""
    value: str
    """The normalized value: an e-mail address in lowercase, a phone number in E.164 (`+491711234567`)."""
    label: str | None
    """Free-text note telling contact infos of the same type apart, e.g. `work`."""

    @classmethod
    def from_model(cls, contact_info: ContactInfo) -> Self:
        return cls(id=contact_info.id, type=contact_info.type, value=contact_info.value, label=contact_info.label)


class ContactInfoCreateRequest(ApiModel):
    """A contact info to attach to a party."""

    type: ContactInfoType = Field(examples=[ContactInfoType.EMAIL])
    """Kind of the contact info; it cannot be changed later."""
    value: str = Field(max_length=MAX_CONTACT_VALUE_LENGTH, examples=["max.mustermann@example.com"])
    """An e-mail address (stored in lowercase) or a phone number, matching `type`.

    A phone number is stored in E.164 (`+491711234567`); one written in its national form (`0171 1234567`) is read
    as a German number. An extension is not supported - put it into `label`.
    """
    label: Name | None = Field(None, examples=["private"])
    """Free-text note telling contact infos of the same type apart, e.g. `work`."""

    @field_validator("value")
    @classmethod
    def _normalize_value(cls, value: str, info: ValidationInfo) -> str:
        # `type` is declared first, so it is validated first; if it was rejected there is nothing to check against.
        type = info.data.get("type")
        return normalize_contact_value(type, value) if type is not None else value

    def to_input(self) -> NewContactInfo:
        return NewContactInfo(type=self.type, value=self.value, label=self.label)


# The type of a stored contact info is only known from the database, so the update model checks
# what needs none (error rule I-2) and the service checks the value against the stored type.
ContactValue = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_CONTACT_VALUE_LENGTH),
    AfterValidator(require_storable_text),
]


class ContactInfoUpdateRequest(ApiModel):
    """Body of `PATCH /parties/{party_id}/contact-infos/{contact_info_id}`: only the fields that are sent change.

    `type` is immutable and not part of this body; delete the contact info and add another one instead.
    """

    value: ContactValue | MISSING = MISSING
    """New value. It must fit the stored type (422 `invalid_contact_value` otherwise) and is normalized as on create:
    an e-mail address to lowercase, a phone number to E.164."""
    label: Name | None | MISSING = MISSING
    """New label; `null` clears it."""


def _reject_duplicate_contact_infos(contact_infos: list[ContactInfoCreateRequest]) -> list[ContactInfoCreateRequest]:
    keys = [(contact_info.type, contact_info.value) for contact_info in contact_infos]
    if len(set(keys)) != len(keys):
        raise ValueError("The same type and value must not appear twice")
    return contact_infos


NewContactInfos = Annotated[list[ContactInfoCreateRequest], AfterValidator(_reject_duplicate_contact_infos)]


# --- Parties: the read side ---


class PartyListItem(ApiModel):
    """A party as it appears in lists and on the other side of a relation."""

    id: uuid.UUID
    """ID of the party."""
    type: PartyType
    """Whether the party is a person or a company."""
    display_name: str
    """First and last name of a person, or the name of a company."""
    roles: list[PartyRole]
    """The roles a person holds; always empty for a company."""
    updated_at: datetime
    """When anything in the party last changed - the person or company, a role, a contact info, a relation - or a
    party related to it was deleted. The start of the writing transaction: pull as `updated_since` describes."""

    @classmethod
    def from_model(cls, party: Party) -> Self:
        return cls(
            id=party.id,
            type=party.type,
            display_name=_display_name(party),
            roles=_roles(party),
            updated_at=party.updated_at,
        )


class RelationResponse(ApiModel):
    """A relation, seen from the party in the path."""

    type: PartyRelationType
    """Type of the relation."""
    direction: RelationDirection
    """`outgoing` if the party in the path is the one the relation starts at, `incoming` if it points to it."""
    party: PartyListItem
    """The party on the other side of the relation."""
    created_at: datetime
    """When the relation was created."""

    @classmethod
    def from_view(cls, view: PartyRelationView) -> Self:
        return cls(
            type=view.type,
            direction=view.direction,
            party=PartyListItem.from_model(view.party),
            created_at=view.created_at,
        )


class StudentRole(ApiModel):
    """The student role of a person."""

    preferred_meeting_tool: PreferredMeetingTool
    """How the student prefers to meet their tutor."""
    subjects: list[SubjectResponse]
    """The subjects the student takes lessons in, ordered by title."""

    @classmethod
    def from_model(cls, student: Student) -> Self:
        return cls(
            preferred_meeting_tool=student.preferred_meeting_tool,
            subjects=_subjects(link.subject for link in student.student_subjects),
        )


class TutorRole(ApiModel):
    """The tutor role of a person."""

    subjects: list[SubjectResponse]
    """The subjects the tutor teaches, ordered by title."""

    @classmethod
    def from_model(cls, tutor: Tutor) -> Self:
        return cls(subjects=_subjects(link.subject for link in tutor.tutor_subjects))


class PersonDetail(ApiModel):
    """A party of type `person` with everything that belongs to it."""

    type: Literal[PartyType.PERSON]
    """Discriminator: always `person`."""
    id: uuid.UUID
    """ID of the party."""
    display_name: str
    """First and last name."""
    firstname: str
    """First name of the person."""
    lastname: str
    """Last name of the person."""
    student: StudentRole | None
    """The student role, or `null` if the person is not a student."""
    tutor: TutorRole | None
    """The tutor role, or `null` if the person is not a tutor."""
    contact_infos: list[ContactInfoResponse]
    """Contact infos of the party, ordered by type and value."""
    created_at: datetime
    """When the party was created."""
    updated_at: datetime
    """When anything in the party - the person, a role, a contact info, a relation - last changed."""

    @classmethod
    def from_model(cls, party: Party) -> Self:
        person = party.person
        assert person is not None, "a party of type person has a person row"
        return cls(
            type=PartyType.PERSON,
            id=party.id,
            display_name=_person_display_name(person),
            firstname=person.firstname,
            lastname=person.lastname,
            student=StudentRole.from_model(person.student) if person.student is not None else None,
            tutor=TutorRole.from_model(person.tutor) if person.tutor is not None else None,
            contact_infos=_contact_infos(party),
            created_at=party.created_at,
            updated_at=party.updated_at,
        )


class CompanyDetail(ApiModel):
    """A party of type `company` with everything that belongs to it."""

    type: Literal[PartyType.COMPANY]
    """Discriminator: always `company`."""
    id: uuid.UUID
    """ID of the party."""
    display_name: str
    """Name of the company."""
    name: str
    """Name of the company."""
    contact_infos: list[ContactInfoResponse]
    """Contact infos of the party, ordered by type and value."""
    created_at: datetime
    """When the party was created."""
    updated_at: datetime
    """When anything in the party - the company, a contact info, a relation - last changed."""

    @classmethod
    def from_model(cls, party: Party) -> Self:
        company = party.company
        assert company is not None, "a party of type company has a company row"
        return cls(
            type=PartyType.COMPANY,
            id=party.id,
            display_name=company.name,
            name=company.name,
            contact_infos=_contact_infos(party),
            created_at=party.created_at,
            updated_at=party.updated_at,
        )


# A PEP 695 alias becomes the named `PartyDetail` schema (`oneOf` + discriminator). The generated
# client ignores the discriminator and tries the variants in order, which works because the two
# members have different required fields - keep it that way.
type PartyDetail = Annotated[PersonDetail | CompanyDetail, Field(discriminator="type")]


def party_detail(party: Party) -> PersonDetail | CompanyDetail:
    """Map a party loaded through ``PARTY_GRAPH`` to the detail of its type."""
    match party.type:
        case PartyType.PERSON:
            return PersonDetail.from_model(party)
        case PartyType.COMPANY:
            return CompanyDetail.from_model(party)
        case _:
            assert_never(party.type)


def _person_display_name(person: Person) -> str:
    return f"{person.firstname} {person.lastname}"


def _display_name(party: Party) -> str:
    match party.type:
        case PartyType.PERSON:
            assert party.person is not None, "a party of type person has a person row"
            return _person_display_name(party.person)
        case PartyType.COMPANY:
            assert party.company is not None, "a party of type company has a company row"
            return party.company.name
        case _:
            assert_never(party.type)


def _roles(party: Party) -> list[PartyRole]:
    person = party.person
    if person is None:
        return []

    held = {PartyRole.STUDENT: person.student, PartyRole.TUTOR: person.tutor}
    return [role for role, row in held.items() if row is not None]


def _contact_infos(party: Party) -> list[ContactInfoResponse]:
    ordered = sorted(party.contact_infos, key=lambda contact_info: (contact_info.type.value, contact_info.value))
    return [ContactInfoResponse.from_model(contact_info) for contact_info in ordered]


def _subjects(subjects: Iterable[Subject]) -> list[SubjectResponse]:
    ordered = sorted(subjects, key=lambda subject: (subject.title.lower(), subject.id))
    return [SubjectResponse.from_model(subject) for subject in ordered]


# --- Persons and companies: the write side ---


# core.subject.id is a Postgres INTEGER: anything outside its range cannot be a subject - nor be bound to a query.
# The size is bounded as well: every ID becomes a bind parameter, and a statement takes 32767 of them.
MAX_SUBJECT_IDS = 100
SubjectIds = Annotated[
    set[Annotated[int, Field(ge=1, le=MAX_SUBJECT_ID)]],
    Field(max_length=MAX_SUBJECT_IDS, examples=[[1, 2]]),
]


class StudentRoleRequest(ApiModel):
    """Body of `PUT /persons/{party_id}/student`, and the `student` of `POST /persons`."""

    preferred_meeting_tool: PreferredMeetingTool = Field(examples=[PreferredMeetingTool.DISCORD])
    """How the student prefers to meet their tutor."""
    subject_ids: SubjectIds = Field(default_factory=set)
    """IDs of the subjects the student takes lessons in. Replaces the whole set; duplicates collapse."""

    def to_input(self) -> StudentRoleData:
        return StudentRoleData(
            preferred_meeting_tool=self.preferred_meeting_tool, subject_ids=frozenset(self.subject_ids)
        )


class TutorRoleRequest(ApiModel):
    """Body of `PUT /persons/{party_id}/tutor`, and the `tutor` of `POST /persons`."""

    subject_ids: SubjectIds = Field(default_factory=set)
    """IDs of the subjects the tutor teaches. Replaces the whole set; duplicates collapse."""

    def to_input(self) -> TutorRoleData:
        return TutorRoleData(subject_ids=frozenset(self.subject_ids))


class PersonCreateRequest(ApiModel):
    """Body of `POST /persons`."""

    firstname: Name = Field(examples=["Max"])
    """First name. Surrounding whitespace is stripped."""
    lastname: Name = Field(examples=["Mustermann"])
    """Last name. Surrounding whitespace is stripped."""
    contact_infos: NewContactInfos = Field(
        default_factory=list,
        examples=[[{"type": "email", "value": "max.mustermann@example.com", "label": "private"}]],
    )
    """Contact infos to create with the person. The same `type` and `value` must not appear twice."""
    student: StudentRoleRequest | None = Field(
        None, examples=[{"preferred_meeting_tool": "discord", "subject_ids": [1, 2]}]
    )
    """Give the person the student role right away. Omit it or send `null` for none."""
    tutor: TutorRoleRequest | None = Field(None, examples=[None])
    """Give the person the tutor role right away. Omit it or send `null` for none."""

    def contact_info_inputs(self) -> list[NewContactInfo]:
        return [contact_info.to_input() for contact_info in self.contact_infos]


class PersonUpdateRequest(ApiModel):
    """Body of `PATCH /persons/{party_id}`: only the fields that are sent change."""

    firstname: Name | MISSING = MISSING
    """New first name. Surrounding whitespace is stripped."""
    lastname: Name | MISSING = MISSING
    """New last name. Surrounding whitespace is stripped."""


class CompanyCreateRequest(ApiModel):
    """Body of `POST /companies`."""

    name: Name = Field(examples=["Musterfirma GmbH"])
    """Name of the company. Surrounding whitespace is stripped."""
    contact_infos: NewContactInfos = Field(
        default_factory=list,
        examples=[[{"type": "email", "value": "office@musterfirma.example", "label": "office"}]],
    )
    """Contact infos to create with the company. The same `type` and `value` must not appear twice."""

    def contact_info_inputs(self) -> list[NewContactInfo]:
        return [contact_info.to_input() for contact_info in self.contact_infos]


class CompanyUpdateRequest(ApiModel):
    """Body of `PATCH /companies/{party_id}`: only the fields that are sent change."""

    name: Name | MISSING = MISSING
    """New name of the company. Surrounding whitespace is stripped."""
