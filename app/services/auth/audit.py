import uuid
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.principal import Principal, PrincipalType
from app.core.db.models import AuthAuditLog


class Operator(StrEnum):
    """Who asks when no token does: the operator commands."""

    CLI = "cli"


type Actor = Principal | Operator
"""Who asked for a change to an account: the principal of a request, or an operator command."""


def format_actor(actor: Actor) -> str:
    """Name ``actor`` the way an audit ``detail`` and ``user_action_token.issued_by`` record it:
    ``<principal_type>:<principal_id>``, or the operator command (``cli``)."""
    match actor:
        case Principal():
            return f"{actor.principal_type}:{actor.principal_id}"
        case Operator():
            return actor.value


class AuditEventType(StrEnum):
    """What an ``AuthAuditLog`` entry records. A ``detail`` never carries a secret or an e-mail address."""

    APPLICATION_CLIENT_CREATED = "application_client.created"
    APPLICATION_CLIENT_DISABLED = "application_client.disabled"
    APPLICATION_CLIENT_UPDATED = "application_client.updated"
    CLIENT_SECRET_CREATED = "client_secret.created"
    CLIENT_SECRET_REVOKED = "client_secret.revoked"
    SCOPE_GRANT_ADDED = "scope_grant.added"
    SCOPE_GRANT_REMOVED = "scope_grant.removed"
    TOKEN_DENIED = "token.denied"
    TOKEN_ISSUED = "token.issued"
    USER_ACCOUNT_CREATED = "user_account.created"
    USER_ACCOUNT_UPDATED = "user_account.updated"
    USER_ACCOUNT_DISABLED = "user_account.disabled"
    USER_ACCOUNT_ENABLED = "user_account.enabled"
    USER_ROLE_ADDED = "user_role.added"
    USER_ROLE_REMOVED = "user_role.removed"
    INVITATION_ISSUED = "invitation.issued"
    PASSWORD_RESET_ISSUED = "password_reset.issued"
    PASSWORD_SET = "password.set"
    SESSION_REVOKED = "session.revoked"
    SESSION_REUSE_DETECTED = "session.reuse_detected"
    DISCORD_LINK_ADDED = "discord_link.added"
    DISCORD_LINK_REACTIVATED = "discord_link.reactivated"
    DISCORD_LINK_MOVED = "discord_link.moved"
    DISCORD_LINK_REMOVED = "discord_link.removed"


async def write_auth_audit_log(
    session: AsyncSession,
    *,
    principal_type: str | None,
    principal_id: str | uuid.UUID | None,
    event_type: str,
    success: bool,
    detail: str | None = None,
) -> None:
    session.add(
        AuthAuditLog(
            principal_type=principal_type,
            principal_id=str(principal_id) if principal_id is not None else None,
            event_type=event_type,
            success=success,
            detail=detail,
        )
    )
    await session.flush()


async def write_user_account_audit_log(
    session: AsyncSession,
    user_id: uuid.UUID,
    event_type: AuditEventType,
    what: str,
    *,
    actor: Actor,
) -> None:
    """Record what happened to a user account: ``detail`` reads ``<what> by <actor>.``

    The account is the subject of the entry, the way the client services record the client they changed.
    ``what`` never names a secret or an e-mail address.
    """
    await write_auth_audit_log(
        session,
        principal_type=PrincipalType.USER,
        principal_id=user_id,
        event_type=event_type,
        success=True,
        detail=f"{what} by {format_actor(actor)}.",
    )


class AuditSubjectType(StrEnum):
    """Subjects of an audit entry that are not principals: ``principal_type`` of their entries."""

    DISCORD_USER = "discord_user"


async def write_discord_link_audit_log(
    session: AsyncSession,
    discord_user_id: int,
    event_type: AuditEventType,
    what: str,
    *,
    actor: Actor,
) -> None:
    """Record what happened to a Discord link: the Discord user is the subject, ``detail`` reads ``<what> by <actor>.``

    ``what`` names the party, both parties on a move, and never a secret.
    """
    await write_auth_audit_log(
        session,
        principal_type=AuditSubjectType.DISCORD_USER,
        principal_id=str(discord_user_id),
        event_type=event_type,
        success=True,
        detail=f"{what} by {format_actor(actor)}.",
    )
