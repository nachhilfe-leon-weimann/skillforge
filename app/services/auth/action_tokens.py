"""One-time tokens: issuing an invitation, a password reset or a Discord link code; redeeming; invalidating.

A token is stored as its ``digest`` only; the plaintext exists once, in the result of the call that
issues it, and never reaches a log, an audit ``detail`` or an error. Purposes stay apart both ways: the password
redemption below takes invitations and resets only, the link code's redemption in ``discord_link_codes`` link
codes only. Both look a token up with ``find_live_action_token`` and spend it with ``spend_action_token``.
"""

import uuid
from collections.abc import Collection, Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.config import AuthSettings
from app.core.auth.passwords import meets_password_policy
from app.core.auth.secrets import digest, generate_secret, hash_secret_async
from app.core.db.models import UserAccount, UserAccountStatus, UserActionToken, UserActionTokenPurpose

from .accounts import lock_user_account
from .audit import Actor, AuditEventType, format_actor, write_user_account_audit_log
from .errors import InvalidActionTokenError, UserAccountNotFoundError, UserAccountStateError, WeakPasswordError
from .results import IssuedActionToken
from .sessions import SessionRevokedReason, revoke_sessions

ACTION_TOKEN_PREFIX = "sf_ua_"

PASSWORD_PURPOSES: frozenset[UserActionTokenPurpose] = frozenset({
    UserActionTokenPurpose.INVITATION,
    UserActionTokenPurpose.PASSWORD_RESET,
})
"""The purposes that set a password at ``/password/redeem`` - and that a change of the e-mail address invalidates.
A Discord link code is neither: it never sets a password, and the address has nothing to do with it."""


async def issue_action_token(
    session: AsyncSession,
    settings: AuthSettings,
    *,
    user_id: uuid.UUID,
    purpose: UserActionTokenPurpose,
    actor: Actor,
) -> IssuedActionToken:
    """Issue a one-time token and invalidate the account's earlier unused ones of that purpose.

    An invitation needs an e-mail address and no password yet, a reset needs a password, a Discord link
    code an ``active`` account. The account row is locked for the whole operation: two requests arriving
    together would otherwise both invalidate what they found and then both insert, leaving two live tokens.
    """
    account = await lock_user_account(session, user_id)
    _require_issuable(account, purpose)
    await invalidate_action_tokens(session, user_id, purposes=[purpose])

    plaintext = generate_secret(ACTION_TOKEN_PREFIX)
    token = UserActionToken(
        user_account_id=user_id,
        purpose=purpose,
        token_hash=digest(plaintext),
        expires_at=datetime.now(UTC) + timedelta(hours=_expire_hours(settings, purpose)),
        issued_by=format_actor(actor),
    )
    session.add(token)
    await session.flush()
    await write_user_account_audit_log(
        session, user_id, _issued_event(purpose), f"Issued the {_name(purpose)}", actor=actor
    )

    return IssuedActionToken(plaintext=plaintext, token=token)


async def redeem_action_token(session: AsyncSession, *, plaintext: str, new_password: str, actor: Actor) -> UserAccount:
    """Set the account's password with an invitation or a password reset.

    Clears the failed-login counter and the lock and marks the token used; a ``password_reset``
    revokes every session of the account. The status does not change: a ``disabled`` account stays
    disabled. A token that is not live, one of another purpose - a Discord link code - and one whose account
    is gone are one ``InvalidActionTokenError``.
    """
    found = await find_live_action_token(session, plaintext, purposes=PASSWORD_PURPOSES)
    if not meets_password_policy(new_password):
        raise WeakPasswordError("The password violates the policy")

    # Hashed before the lock: Argon2 is slow by design, and every other issue or redeem on this
    # account would queue behind it. The re-read under the lock still decides whether it is stored.
    password_hash = await hash_secret_async(new_password)
    account = await spend_action_token(session, found)
    account.password_hash = password_hash
    account.failed_login_count = 0
    account.locked_until = None
    await write_user_account_audit_log(
        session,
        account.id,
        AuditEventType.PASSWORD_SET,
        f"Set the password with the {_name(found.purpose)}",
        actor=actor,
    )
    if found.purpose is UserActionTokenPurpose.PASSWORD_RESET:
        await revoke_sessions(session, account.id, reason=SessionRevokedReason.PASSWORD_RESET, actor=actor)

    return account


async def find_live_action_token(
    session: AsyncSession, plaintext: str, *, purposes: Collection[UserActionTokenPurpose]
) -> UserActionToken:
    """Return the live token of one of ``purposes`` behind ``plaintext``: the cheap look-up, without a lock.

    A token that was never live costs neither a hash nor a row lock. Unknown, used, invalidated, expired or of
    another purpose is one ``InvalidActionTokenError`` - a caller cannot tell them apart. The answer is final only
    once ``spend_action_token`` has read the token again under its account's lock.
    """
    found = await session.scalar(
        select(UserActionToken).where(
            UserActionToken.token_hash == digest(plaintext), UserActionToken.purpose.in_(list(purposes))
        )
    )
    if found is None or not _is_live(found, datetime.now(UTC)):
        raise InvalidActionTokenError("Unknown, used, invalidated or expired token")
    return found


async def spend_action_token(session: AsyncSession, token: UserActionToken) -> UserAccount:
    """Mark ``token`` used and return its account, locked until the transaction ends.

    Only under that lock is the answer authoritative: a redeem or an issue that ran at the same time may have spent
    or replaced the token since the look-up. The locked account keeps the row alive. An account gone since the
    look-up - its party was deleted, and its tokens with it - is ``InvalidActionTokenError`` too. A caller that
    fails afterwards raises, and the rollback of its transaction leaves the token live.
    """
    try:
        account = await lock_user_account(session, token.user_account_id)
    except UserAccountNotFoundError:
        raise InvalidActionTokenError("The token's account is gone") from None
    await session.refresh(token, with_for_update=True)
    now = datetime.now(UTC)
    if not _is_live(token, now):
        raise InvalidActionTokenError("Unknown, used, invalidated or expired token")

    token.used_at = now
    return account


async def invalidate_action_tokens(
    session: AsyncSession, user_id: uuid.UUID, *, purposes: Iterable[UserActionTokenPurpose]
) -> None:
    """Make the account's unused tokens of ``purposes`` stop working; a used token stays as it was."""
    await session.execute(
        update(UserActionToken)
        .where(
            UserActionToken.user_account_id == user_id,
            UserActionToken.purpose.in_(list(purposes)),
            UserActionToken.used_at.is_(None),
            UserActionToken.invalidated_at.is_(None),
        )
        .values(invalidated_at=datetime.now(UTC))
    )


def _is_live(token: UserActionToken, now: datetime) -> bool:
    """Whether the token can still be redeemed: not used, not invalidated, not expired."""
    return token.used_at is None and token.invalidated_at is None and token.expires_at > now


def _require_issuable(account: UserAccount, purpose: UserActionTokenPurpose) -> None:
    """An invitation sets up the password login, so it needs an e-mail address and no password yet; a
    reset needs a password; a Discord link code makes a credential for the bot, so it needs an active account."""
    match purpose:
        case UserActionTokenPurpose.INVITATION if account.email is None:
            raise UserAccountStateError(f"User account {account.id} has no e-mail address")
        case UserActionTokenPurpose.INVITATION if account.password_hash is not None:
            raise UserAccountStateError(f"User account {account.id} already has a password")
        case UserActionTokenPurpose.PASSWORD_RESET if account.password_hash is None:
            raise UserAccountStateError(f"User account {account.id} has no password yet")
        case UserActionTokenPurpose.DISCORD_LINK if account.status is not UserAccountStatus.ACTIVE:
            raise UserAccountStateError(f"User account {account.id} is disabled")


def _expire_hours(settings: AuthSettings, purpose: UserActionTokenPurpose) -> int:
    match purpose:
        case UserActionTokenPurpose.INVITATION:
            return settings.invitation_expire_hours
        case UserActionTokenPurpose.PASSWORD_RESET:
            return settings.password_reset_expire_hours
        case UserActionTokenPurpose.DISCORD_LINK:
            return settings.discord_link_code_expire_hours


def _issued_event(purpose: UserActionTokenPurpose) -> AuditEventType:
    match purpose:
        case UserActionTokenPurpose.INVITATION:
            return AuditEventType.INVITATION_ISSUED
        case UserActionTokenPurpose.PASSWORD_RESET:
            return AuditEventType.PASSWORD_RESET_ISSUED
        case UserActionTokenPurpose.DISCORD_LINK:
            return AuditEventType.DISCORD_LINK_CODE_ISSUED


def _name(purpose: UserActionTokenPurpose) -> str:
    """The token as an audit ``detail`` names it: ``invitation token``, ``password reset token``, ``Discord link
    code``."""
    match purpose:
        case UserActionTokenPurpose.DISCORD_LINK:
            return "Discord link code"
        case _:
            return f"{purpose.replace('_', ' ')} token"
