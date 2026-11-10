"""What an integration call can end in besides success.

These errors are internal: they say what happened on the wire, not what an API client did wrong, so they
are no ``DomainError`` and have no HTTP mapping. The service that makes the call decides what follows -
record an unknown outcome for a later reconcile, or raise its own domain error. No message carries a
header, a token or a body; a remote error text is kept in ``remote_message``, never in ``str()``.
"""


class IntegrationError(Exception):
    """Base class of every integration failure; ``status`` is the HTTP status, if a response came back."""

    def __init__(self, message: str, *, status: int | None = None, remote_message: str | None = None) -> None:
        super().__init__(message if status is None else f"{message} (HTTP {status})")
        self.status = status
        self.remote_message = remote_message


class IntegrationRefusedError(IntegrationError):
    """A guard refused the call before anything was sent."""


class IdentityCheckError(IntegrationRefusedError):
    """The configured account could not be confirmed; nothing but the identity read was sent."""


class IntegrationRejectedError(IntegrationError):
    """The remote system refused the request (4xx); it changed nothing."""


class IntegrationUnavailableError(IntegrationError):
    """A read failed (transport error or 5xx); it changed nothing and may be repeated."""


class UnexpectedResponseError(IntegrationError):
    """A read answered with a body the models do not accept."""


class OutcomeUnknownError(IntegrationError):
    """A write may or may not have taken effect: never repeat it, reconcile it.

    Raised for a transport error, a 3xx or a 5xx once a write was handed to the network, for any failure while
    its answer is handled, and for a 2xx write whose body cannot be read.
    """
