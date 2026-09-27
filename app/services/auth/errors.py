from app.core.auth.passwords import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from app.core.errors import ConflictError, DomainValidationError, NotFoundError


class ClientCredentialsError(ValueError):
    """Raised when client credentials cannot be exchanged for an access token.

    Deliberately outside the error taxonomy: the OAuth2 statuses and codes of these errors (400/401,
    ``invalid_client``, ``invalid_scope``) are declared by the auth API in ``app/api/v1/auth/errors.py``.
    """


class InvalidClientCredentialsError(ClientCredentialsError):
    """Raised for unknown clients, disabled clients, or invalid client secrets."""


class InvalidClientScopeError(ClientCredentialsError):
    """Raised when requested scopes are unknown, inactive, or not granted to the client."""


class ApplicationClientManagementError(ValueError):
    """Raised when application client management cannot be completed."""


class ApplicationClientAlreadyExistsError(ApplicationClientManagementError, ConflictError):
    """Raised when creating a client with an existing client_id."""

    message = "Application client already exists"


class ApplicationClientNotFoundError(ApplicationClientManagementError, NotFoundError):
    """Raised when an application client cannot be found."""

    message = "Application client not found"


class ApplicationClientSecretNotFoundError(ApplicationClientManagementError, NotFoundError):
    """Raised when an application client secret cannot be found."""

    message = "Application client secret not found"


class ApplicationClientScopeGrantNotFoundError(ApplicationClientManagementError, NotFoundError):
    """Raised when a scope grant cannot be found."""

    message = "Application client scope grant not found"


class UserAccountManagementError(ValueError):
    """Raised when managing a user account cannot be completed.

    The catalog below is closed (user-authentication spec): a requirement that seems to need another
    class or ``code`` is a reason to stop and ask, not to add one.
    """


class UserAccountNotFoundError(UserAccountManagementError, NotFoundError):
    """Raised when no user account has the given ID."""

    message = "User account not found"


class UserAccountAlreadyExistsError(UserAccountManagementError, ConflictError):
    """Raised when the party already has a user account."""

    message = "The party already has a user account"


class UserEmailAlreadyInUseError(UserAccountManagementError, ConflictError):
    """Raised when another user account uses the e-mail address."""

    message = "Another user account uses this e-mail address"


class UserAccountStateError(UserAccountManagementError, ConflictError):
    """Raised when the account's state does not allow what was asked.

    An invitation for an account without an e-mail address or with a password, a reset for one without
    a password, and removing the e-mail address of an account that has a password.
    """

    message = "The state of the user account does not allow this"


class UnknownAccountPartyError(UserAccountManagementError, DomainValidationError):
    """Raised when the party a new user account names does not exist: a body reference, hence 422."""

    message = "Unknown party"


class AccountPartyNotAPersonError(UserAccountManagementError, DomainValidationError):
    """Raised when the party a new user account names is a company (user-authentication spec, decision C)."""

    message = "A user account belongs to a person, not to a company"


class UserRoleNotFoundError(UserAccountManagementError, NotFoundError):
    """Raised when removing a stored role the account does not hold."""

    message = "The user account does not hold this role"


class InvalidActionTokenError(UserAccountManagementError, DomainValidationError):
    """Raised for an unknown, used, invalidated or expired one-time token.

    One error for all four: a caller cannot tell them apart (no enumeration).
    """

    message = "Invalid or expired token"


class WeakPasswordError(UserAccountManagementError, DomainValidationError):
    """Raised when a password violates the policy in ``passwords.py``."""

    message = f"Password must be text of {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters"


class DiscordLinkManagementError(ValueError):
    """Raised when managing a Discord link cannot be completed.

    The catalog below is closed (bot-decoupling spec): a requirement that seems to need another class or
    ``code`` is a reason to stop and ask, not to add one.
    """


class DiscordLinkNotFoundError(DiscordLinkManagementError, NotFoundError):
    """Raised when no link, active or not, exists for the Discord user."""

    message = "Discord link not found"


class DiscordAccountAlreadyLinkedError(DiscordLinkManagementError, ConflictError):
    """Raised when the Discord user is actively linked to another party: unlink it there first."""

    message = "The Discord account is linked to another party"


class UnknownLinkPartyError(DiscordLinkManagementError, DomainValidationError):
    """Raised when the party a link names does not exist: a body reference, hence 422."""

    message = "Unknown party"


class LinkPartyNotAPersonError(DiscordLinkManagementError, DomainValidationError):
    """Raised when the party a link names is a company."""

    message = "A Discord link belongs to a person, not to a company"
