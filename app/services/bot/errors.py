"""Domain errors of the bot services.

Each error derives from a category of the taxonomy in ``app/core/errors.py``, which decides its
HTTP status in the API layer. ``message`` is the text a client sees; the instance message a service
raises with (``JobNotFoundError(f"No job with id {job_id}")``) stays internal unless the class sets
``expose_message``.
"""

from app.core.errors import ConflictError, DomainValidationError, NotFoundError

# The CRM owns parties (ADR 0007): linking a Discord account to an unknown party raises the CRM's error.
from app.services.crm.errors import PartyNotFoundError as PartyNotFoundError


class BotServiceError(Exception):
    """Base class for bot service-layer domain errors."""


class PrincipalNotFoundError(BotServiceError, NotFoundError):
    """No Discord user is registered for the requested discord_id."""

    message = "Discord principal not found"


class TutorContextNotFoundError(BotServiceError, NotFoundError):
    """No tutor workspace exists for the requested guild/tutor."""

    message = "Tutor context not found"


class StudentContextNotFoundError(BotServiceError, NotFoundError):
    """No student workspace exists for the requested guild/student."""

    message = "Student context not found"


class CommandEnvNotFoundError(BotServiceError, NotFoundError):
    """No command env channel matches the requested coordinates."""

    message = "Command env channel not found"


class CommandEnvValidationError(BotServiceError, DomainValidationError):
    """The referenced guild/channel/owner for a command env does not exist or is inconsistent."""

    message = "Command env references an unknown guild, channel, or owner"


class CommandEnvConflictError(BotServiceError, ConflictError):
    """The command env violates a uniqueness rule (e.g. owner already owns one of this kind)."""

    message = "Owner already owns a command env of this kind in the guild"


class PermissionGroupNotFoundError(BotServiceError, NotFoundError):
    """No permission group exists for the requested group_key."""

    message = "Permission group not found"


class GroupMembershipNotFoundError(BotServiceError, NotFoundError):
    """The Discord user is not a member of the requested permission group."""

    message = "Group membership not found"


class JobNotFoundError(BotServiceError, NotFoundError):
    """No job exists for the requested job_id."""

    message = "Job not found"


class JobNotClaimedError(BotServiceError, ConflictError):
    """The job is not in the claimed state required for completion/failure."""

    message = "Job is not in a claimed state"


class JobNotFailedError(BotServiceError, ConflictError):
    """The job is not in the failed state required for an operator requeue."""

    message = "Job is not in a failed state"


class OperationNotFoundError(BotServiceError, NotFoundError):
    """No operation exists for the requested operation_id (and kind)."""

    message = "Operation not found"


class OperationNotPendingError(BotServiceError, ConflictError):
    """The operation is not in the PREPARED state (already committed/failed, or expired)."""

    message = "Operation is not in a prepared state (already committed, failed, or expired)"
    # Raised with client-ready reasons only ("Operation has expired").
    expose_message = True


class TransitionValidationError(BotServiceError, DomainValidationError):
    """A transition precondition failed (role, active flag, guild/channel context, missing entity)."""

    message = "Transition validation failed"
    # Raised with client-ready reasons only ("Tutor has no workspace in this guild").
    expose_message = True


class TransitionConflictError(BotServiceError, ConflictError):
    """A transition conflicts with current state (already exists, capacity reached, wrong channel state)."""

    message = "Transition conflict"
    # Raised with client-ready reasons only ("Tutor student capacity reached").
    expose_message = True
