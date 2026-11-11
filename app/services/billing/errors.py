"""Domain errors of the billing services: the closed catalog of the billing spec ("Error catalog").

Each class derives from a category of the taxonomy in ``app/core/errors.py``, which decides its HTTP status, and
gets its ``code`` from the class name. A slice adds the catalog classes it raises first; none is added beyond the
catalog. No message repeats a name, a calendar title or a sevDesk id.
"""

from app.core.errors import ConflictError, DomainValidationError, NotFoundError


class LessonNotFoundError(NotFoundError):
    """No lesson exists for the requested lesson_id."""

    message = "Lesson not found"


class LessonAlreadyExistsError(ConflictError):
    """The student already has a lesson starting at this instant (``uq_lesson_student_start``)."""

    message = "The student already has a lesson at this start"


class RateNotFoundError(NotFoundError):
    """The student has no rate taking effect on the requested day."""

    message = "Rate not found"


class UnknownStudentError(DomainValidationError):
    """A party named as a student holds no student role. A body reference that names no party at all is one too
    (422, not 404); a party in a path that does not exist is the CRM's ``PartyNotFoundError``."""

    message = "Unknown student"


class RateNotQuarterHourError(DomainValidationError):
    """A rate breaks rule G: cents per hour must be 0 or more and divisible by 4, so every quarter hour is a whole
    cent (billing spec, decision G and PD3)."""

    message = "A rate must be at least 0 cents per hour and divisible by 4"


class LessonNotBillableError(DomainValidationError):
    """``billable`` was set on a lesson that is ``planned`` or ``not_held``; only ``held`` and ``cancelled`` can be
    billable."""

    message = "Only a held or cancelled lesson can be billable"
