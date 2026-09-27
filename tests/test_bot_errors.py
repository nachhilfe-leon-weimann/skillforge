import pytest

from app.api.v1.common import status_for
from app.core.errors import DomainError
from app.services.bot import errors
from app.services.bot.errors import BotServiceError

OPERATION_NOT_PENDING = "Operation is not in a prepared state (already committed, failed, or expired)"

# (error, status, code, public message, expose_message)
CONTRACT = [
    (errors.PrincipalNotFoundError, 404, "principal_not_found", "Discord principal not found", False),
    (errors.TutorContextNotFoundError, 404, "tutor_context_not_found", "Tutor context not found", False),
    (errors.StudentContextNotFoundError, 404, "student_context_not_found", "Student context not found", False),
    (errors.CommandEnvNotFoundError, 404, "command_env_not_found", "Command env channel not found", False),
    (
        errors.CommandEnvValidationError,
        422,
        "command_env_validation",
        "Command env references an unknown guild, channel, or owner",
        False,
    ),
    (
        errors.CommandEnvConflictError,
        409,
        "command_env_conflict",
        "Owner already owns a command env of this kind in the guild",
        False,
    ),
    (errors.PermissionGroupNotFoundError, 404, "permission_group_not_found", "Permission group not found", False),
    (errors.GroupMembershipNotFoundError, 404, "group_membership_not_found", "Group membership not found", False),
    (errors.JobNotFoundError, 404, "job_not_found", "Job not found", False),
    (errors.JobNotClaimedError, 409, "job_not_claimed", "Job is not in a claimed state", False),
    (errors.JobNotFailedError, 409, "job_not_failed", "Job is not in a failed state", False),
    (errors.OperationNotFoundError, 404, "operation_not_found", "Operation not found", False),
    (errors.OperationNotPendingError, 409, "operation_not_pending", OPERATION_NOT_PENDING, True),
    (errors.TransitionValidationError, 422, "transition_validation", "Transition validation failed", True),
    (errors.TransitionConflictError, 409, "transition_conflict", "Transition conflict", True),
]


@pytest.mark.parametrize(("error", "status", "code", "message", "expose_message"), CONTRACT)
def test_bot_error_contract(error: type[DomainError], status: int, code: str, message: str, expose_message: bool):
    assert issubclass(error, BotServiceError)
    assert status_for(error) == status
    assert error.code == code
    assert error.message == message
    assert error.expose_message is expose_message


def test_contract_table_covers_every_bot_error():
    declared = {
        value
        for value in vars(errors).values()
        if isinstance(value, type) and issubclass(value, BotServiceError) and value is not BotServiceError
    }

    assert {error for error, *_ in CONTRACT} == declared


def test_bot_service_error_stays_a_plain_base_without_a_contract_of_its_own():
    assert not issubclass(BotServiceError, DomainError)


def test_party_not_found_is_the_crm_error_not_a_bot_one():
    """The CRM owns parties (ADR 0007); its contract is pinned in ``tests/api/test_crm_error_contract.py``."""
    from app.services.crm.errors import PartyNotFoundError

    assert errors.PartyNotFoundError is PartyNotFoundError
    assert not issubclass(PartyNotFoundError, BotServiceError)
