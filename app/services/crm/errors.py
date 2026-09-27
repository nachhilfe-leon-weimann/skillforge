"""Domain errors of the CRM services.

Each error derives from a category of the taxonomy in ``app/core/errors.py``, which decides its
HTTP status in the API layer, and gets its ``code`` from the class name. ``message`` is the text a
client sees; the instance message a service raises with stays internal unless the class sets
``expose_message``.
"""

from app.core.errors import ConflictError, DomainValidationError, NotFoundError


class PartyNotFoundError(NotFoundError):
    """No party exists for the requested party_id."""

    message = "Party not found"


class PersonNotFoundError(NotFoundError):
    """No person exists for the requested party_id; a company's ID counts as missing here."""

    message = "Person not found"


class CompanyNotFoundError(NotFoundError):
    """No company exists for the requested party_id; a person's ID counts as missing here."""

    message = "Company not found"


class RoleNotFoundError(NotFoundError):
    """The person does not hold the role that was to be removed; one class for both roles."""

    message = "Role not assigned"


class ContactInfoNotFoundError(NotFoundError):
    """The party has no contact info with the requested ID; one of another party counts as missing."""

    message = "Contact info not found"


class ContactInfoAlreadyExistsError(ConflictError):
    """The party already has a contact info with this type and value."""

    message = "Contact info already exists for this party"


class PartyRelationNotFoundError(NotFoundError):
    """No relation of the requested type leads from the party to the other one."""

    message = "Party relation not found"


class RelatedPartyNotFoundError(NotFoundError):
    """The party on the other side of a relation - the second ID in the path - does not exist."""

    message = "Related party not found"


class PartyInUseError(ConflictError):
    """The party is linked to an external system or has an active Discord link and must not be orphaned there."""

    message = "Party is linked to external systems"
    # Raised with a client-ready message only: it names the kinds of links, never their identifiers.
    expose_message = True


class SubjectNotFoundError(NotFoundError):
    """No subject exists for the requested subject_id."""

    message = "Subject not found"


class SubjectAlreadyExistsError(ConflictError):
    """Another subject already carries this title (compared case-insensitively)."""

    message = "Subject already exists"


class SubjectInUseError(ConflictError):
    """The subject is still referenced by a student or tutor role."""

    message = "Subject is still assigned to students or tutors"


class UnknownSubjectError(DomainValidationError):
    """A subject referenced in a request body does not exist (body reference: 422, not 404)."""

    message = "Unknown subject"
    # Raised with a client-ready message only: it lists the unknown IDs the client sent itself.
    expose_message = True


class InvalidPartyRelationError(DomainValidationError):
    """The pair of parties violates the rule of the relation type."""

    message = "Invalid party relation"
    # Raised with a client-ready message only: it names the violated rule, never a party.
    expose_message = True


class InvalidContactValueError(DomainValidationError):
    """The value does not fit the type of the stored contact info (known from the database only)."""

    message = "Value is not valid for this contact info type"
