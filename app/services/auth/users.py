"""User accounts: creating, reading, listing and updating them, and their stored roles.

An account belongs to exactly one person party and is created only through ``auth:users:manage``
(user-authentication spec, decision C). Its one-time tokens live in ``action_tokens``, its sessions in
``sessions``, and reading or locking an account row in ``accounts``.
"""

import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth.inputs import normalize_email
from app.core.db.models import Party, PartyType, UserAccount, UserAccountRole, UserAccountRoleName, UserAccountStatus
from app.core.unset import UNSET, Unset

from .accounts import find_user_account_by_party, get_user_account, lock_user_account
from .action_tokens import PASSWORD_PURPOSES, invalidate_action_tokens
from .audit import Actor, AuditEventType, write_user_account_audit_log
from .errors import (
    AccountPartyNotAPersonError,
    UnknownAccountPartyError,
    UserAccountAlreadyExistsError,
    UserAccountStateError,
    UserEmailAlreadyInUseError,
    UserRoleNotFoundError,
)
from .results import UserAccountWithRoles
from .roles import account_roles, derive_roles_for, stored_roles
from .sessions import SessionRevokedReason, revoke_sessions

EMAIL_CONSTRAINT = "user_account_email_key"
"""The unique constraint on ``user_account.email``: the model and the migration leave it to Postgres to name."""


async def create_user_account(
    session: AsyncSession,
    *,
    party_id: uuid.UUID,
    email: str | None = None,
    roles: Iterable[UserAccountRoleName] = (),
    actor: Actor,
) -> UserAccountWithRoles:
    """Create an ``active`` account for a person party, with or without a login e-mail address.

    The party row is locked first (as the CRM's ``_load_person`` does): a concurrent ``delete_party``
    then ends in ``unknown_account_party`` rather than in a foreign-key error, and a concurrent create
    for the same party waits and finds this account.
    """
    party_type = await session.scalar(select(Party.type).where(Party.id == party_id).with_for_update(key_share=True))
    if party_type is None:
        raise UnknownAccountPartyError(f"No party with id {party_id}")
    if party_type is not PartyType.PERSON:
        raise AccountPartyNotAPersonError(f"Party {party_id} is a company")
    if await find_user_account_by_party(session, party_id) is not None:
        raise UserAccountAlreadyExistsError(f"Party {party_id} already has a user account")

    normalized = None if email is None else normalize_email(email)
    role_names = sorted(set(roles))
    account = UserAccount(
        party_id=party_id,
        email=normalized,
        status=UserAccountStatus.ACTIVE,
        roles=[UserAccountRole(role=role) for role in role_names],
    )
    async with _unique_email(session):
        session.add(account)
    await write_user_account_audit_log(
        session,
        account.id,
        AuditEventType.USER_ACCOUNT_CREATED,
        f"Created the user account of party {party_id} with the stored roles [{', '.join(role_names)}]",
        actor=actor,
    )

    return await load_user_account(session, account.id)


async def load_user_account(session: AsyncSession, user_id: uuid.UUID) -> UserAccountWithRoles:
    """Return the account behind ``user_id`` with its stored *and* derived roles."""
    account = await get_user_account(session, user_id)
    return UserAccountWithRoles(account=account, roles=await account_roles(session, account))


async def list_user_accounts(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    status: UserAccountStatus | None = None,
    party_id: uuid.UUID | None = None,
    email: str | None = None,
) -> tuple[list[UserAccountWithRoles], int]:
    """Return one page of accounts ordered by ``created_at`` and ``id``, plus the total count.

    ``email`` matches exactly, in its canonical form (``normalize_email``). A combination of filters
    nothing matches is an empty page.
    """
    conditions = []
    if status is not None:
        conditions.append(UserAccount.status == status)
    if party_id is not None:
        conditions.append(UserAccount.party_id == party_id)
    if email is not None:
        conditions.append(UserAccount.email == normalize_email(email))

    total = (await session.execute(select(func.count()).select_from(UserAccount).where(*conditions))).scalar_one()
    accounts = list(
        await session.scalars(
            select(UserAccount)
            .where(*conditions)
            .order_by(UserAccount.created_at, UserAccount.id)
            .limit(limit)
            .offset(offset)
            .options(selectinload(UserAccount.roles))
        )
    )
    derived = await derive_roles_for(session, (account.party_id for account in accounts))
    page = [
        UserAccountWithRoles(account=account, roles=stored_roles(account) | derived[account.party_id])
        for account in accounts
    ]
    return page, total


async def update_user_account(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    email: str | None | Unset = UNSET,
    status: UserAccountStatus | None = None,
    actor: Actor,
) -> UserAccountWithRoles:
    """Change the given fields: ``email=UNSET`` and ``status=None`` mean unchanged, ``email=None`` removes it.

    Changing or removing the e-mail address invalidates the account's unused action tokens; removing it
    from an account that has a password is refused, since the password login needs it. Disabling
    revokes every session; enabling is always allowed. Every change gets its audit entry, a request that
    changes nothing records nothing. The row is locked, so a password set at the same time cannot slip
    past the e-mail check.
    """
    account = await lock_user_account(session, user_id)

    if email is not UNSET:
        new_email = None if email is None else normalize_email(email)
        if new_email != account.email:
            await _change_email(session, account, new_email, actor=actor)

    if status is not None and status is not account.status:
        account.status = status
        await write_user_account_audit_log(
            session, user_id, _status_event(status), f"Set the status to {status}", actor=actor
        )
        if status is UserAccountStatus.DISABLED:
            await revoke_sessions(session, user_id, reason=SessionRevokedReason.ACCOUNT_DISABLED, actor=actor)

    return await load_user_account(session, user_id)


async def add_user_role(
    session: AsyncSession, user_id: uuid.UUID, *, role: UserAccountRoleName, actor: Actor
) -> UserAccountWithRoles:
    """Give the account a stored role. Idempotent: a role it already holds changes and records nothing.

    The account row is locked first, so two overlapping requests cannot both insert the same key.
    """
    account = await lock_user_account(session, user_id)
    if role not in {held.role for held in account.roles}:
        account.roles.append(UserAccountRole(role=role))
        await write_user_account_audit_log(
            session, user_id, AuditEventType.USER_ROLE_ADDED, f"Added the role {role}", actor=actor
        )

    return await load_user_account(session, user_id)


async def remove_user_role(
    session: AsyncSession, user_id: uuid.UUID, *, role: UserAccountRoleName, actor: Actor
) -> None:
    """Take a stored role away; a role the account does not hold is ``UserRoleNotFoundError``.

    Locked like ``add_user_role``: the second of two overlapping removals then finds the role gone.
    """
    account = await lock_user_account(session, user_id)
    held = next((stored for stored in account.roles if stored.role is role), None)
    if held is None:
        raise UserRoleNotFoundError(f"User account {user_id} does not hold the role {role}")

    account.roles.remove(held)
    await write_user_account_audit_log(
        session, user_id, AuditEventType.USER_ROLE_REMOVED, f"Removed the role {role}", actor=actor
    )


async def _change_email(session: AsyncSession, account: UserAccount, new_email: str | None, *, actor: Actor) -> None:
    if new_email is None and account.password_hash is not None:
        raise UserAccountStateError(f"User account {account.id} has a password, which needs the e-mail address")

    async with _unique_email(session):
        account.email = new_email
    await invalidate_action_tokens(session, account.id, purposes=PASSWORD_PURPOSES)
    # Never the address itself: the entry names the account, and an address is personal data.
    change = "Removed" if new_email is None else "Changed"
    await write_user_account_audit_log(
        session, account.id, AuditEventType.USER_ACCOUNT_UPDATED, f"{change} the e-mail address", actor=actor
    )


@asynccontextmanager
async def _unique_email(session: AsyncSession) -> AsyncIterator[None]:
    """Write the change made inside the block in a SAVEPOINT and translate an e-mail conflict.

    Uniqueness is decided by the constraint, not by check-then-insert; the flush forces the violation
    here instead of at commit. Any other violation is not an e-mail conflict and is raised as it is. The
    change must happen *inside* the block: ``begin_nested()`` first flushes whatever is pending into the
    enclosing transaction, where a violation would take it down - and its message, which names the
    address, would be logged.
    """
    try:
        async with session.begin_nested():
            yield
            await session.flush()
    except IntegrityError as exc:
        if _violated_constraint(exc) != EMAIL_CONSTRAINT:
            raise
        raise UserEmailAlreadyInUseError("The e-mail address is in use") from exc


def _violated_constraint(exc: IntegrityError) -> str | None:
    """The name of the constraint ``exc`` violated; asyncpg's own error, the cause of the DBAPI one, has it."""
    return getattr(exc.orig.__cause__, "constraint_name", None) if exc.orig is not None else None


def _status_event(status: UserAccountStatus) -> AuditEventType:
    match status:
        case UserAccountStatus.ACTIVE:
            return AuditEventType.USER_ACCOUNT_ENABLED
        case UserAccountStatus.DISABLED:
            return AuditEventType.USER_ACCOUNT_DISABLED
