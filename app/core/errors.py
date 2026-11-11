"""HTTP-agnostic error taxonomy shared by all domains (ADR 0006).

Services raise subclasses of the categories below. They carry a stable ``code`` and a safe public
``message`` but no status code: the mapping to HTTP lives in ``app/api/v1/common/errors.py``.
"""

import re

_WORD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _code_from_class_name(name: str) -> str:
    return _WORD_BOUNDARY.sub("_", name.removesuffix("Error")).lower()


class DomainError(Exception):
    """Base class of every error a service raises on purpose.

    ``code`` is the machine-readable identifier clients branch on. It defaults to the snake-cased
    class name without the ``Error`` suffix (``PartyNotFoundError`` -> ``party_not_found``), so
    renaming a class is a contract change; set ``code`` explicitly to rename a class safely.

    ``message`` is the public default text. The instance message (``str(exc)``) may contain
    internals such as IDs and is only shown to clients if ``expose_message`` is set.
    """

    code: str = "domain_error"
    message: str = "The request could not be completed"
    expose_message: bool = False

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "code" not in cls.__dict__:
            cls.code = _code_from_class_name(cls.__name__)


class NotFoundError(DomainError):
    """The addressed resource does not exist."""

    message = "Resource not found"


class ConflictError(DomainError):
    """The request conflicts with the current state of the resource."""

    message = "Request conflicts with the current state of the resource"


class DomainValidationError(DomainError):
    """The request is well-formed but violates a domain rule."""

    message = "Request violates a domain rule"


class UpstreamError(DomainError):
    """Another system the request needs failed or refused; the request itself may be fine (ADR 0010, point 10)."""

    message = "An upstream system could not be used"
