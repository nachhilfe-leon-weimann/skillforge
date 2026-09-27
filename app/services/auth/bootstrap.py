import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.config import AuthSettings
from app.core.auth.scopes import Scope, parse_scopes
from app.core.db.models import (
    ApplicationClient,
    ApplicationClientStatus,
    GrantMode,
    UserAccountRoleName,
    UserAccountStatus,
    UserActionTokenPurpose,
)

from .accounts import find_user_account_by_party
from .action_tokens import issue_action_token
from .audit import AuditEventType, Operator, write_auth_audit_log
from .clients import find_application_client
from .results import BootstrappedAdminAccount, BootstrappedApplicationClient
from .scopes import grant_client_scopes, seed_default_scopes
from .secrets import client_has_usable_secret, create_client_secret
from .users import add_user_role, create_user_account, update_user_account


async def bootstrap_application_client(
    session: AsyncSession,
    *,
    client_id: str,
    scopes: Iterable[Scope | str],
    mode: GrantMode = GrantMode.APPLICATION,
) -> BootstrappedApplicationClient:
    """Ensure an active client ``client_id`` holding ``scopes`` in ``mode`` and a usable secret.

    A missing client is created and named after its ID. An existing one keeps its name, description,
    grants and secrets; a disabled one is re-enabled, which writes one ``application_client.updated``
    entry - a run that changes nothing writes none.
    """
    await seed_default_scopes(session)

    client = await find_application_client(session, client_id)
    created_client = False
    if client is None:
        client = ApplicationClient(client_id=client_id, name=client_id, status=ApplicationClientStatus.ACTIVE)
        session.add(client)
        await session.flush()
        created_client = True
        await write_auth_audit_log(
            session,
            principal_type="application",
            principal_id=client.id,
            event_type=AuditEventType.APPLICATION_CLIENT_CREATED,
            success=True,
            detail=f"Created application client {client.client_id}",
        )
    else:
        await _ensure_active(session, client)

    requested_scope_keys = parse_scopes(scopes)
    await grant_client_scopes(session, client=client, scope_keys=requested_scope_keys, mode=mode)

    created_secret = None
    if not await client_has_usable_secret(session, client_id=client.id, now=datetime.now(UTC)):
        created_secret = await create_client_secret(
            session,
            application_client_id=client.id,
            label="bootstrap",
        )

    await session.flush()
    return BootstrappedApplicationClient(
        client=client,
        created_client=created_client,
        created_secret=created_secret,
        granted_scopes=requested_scope_keys,
    )


async def _ensure_active(session: AsyncSession, client: ApplicationClient) -> None:
    """Re-enable a disabled ``client`` and record it in one audit entry; an active one is left alone."""
    if client.status is ApplicationClientStatus.ACTIVE:
        return

    client.status = ApplicationClientStatus.ACTIVE
    await write_auth_audit_log(
        session,
        principal_type="application",
        principal_id=client.id,
        event_type=AuditEventType.APPLICATION_CLIENT_UPDATED,
        success=True,
        detail=f"Updated application client {client.client_id}.",
    )


async def bootstrap_admin_account(
    session: AsyncSession,
    settings: AuthSettings,
    *,
    party_id: uuid.UUID,
    email: str,
) -> BootstrappedAdminAccount:
    """Ensure an enabled admin account for a person party with the login ``email``, and a way in.

    The break-glass command (user-authentication spec, decision P): the first admin cannot be created
    through the API, and the only admin cannot reset, re-enable or re-promote themselves. A missing
    account is created; an existing one keeps its id, gets the ``admin`` role, ``status = active`` and
    ``email`` - each change with its audit entry, exactly as through the API. Then it issues an
    invitation while the account has no password, a ``password_reset`` token once it has one; earlier
    unused tokens of that purpose stop working.
    """
    account = await find_user_account_by_party(session, party_id)
    created_account = account is None
    if account is None:
        view = await create_user_account(
            session, party_id=party_id, email=email, roles=[UserAccountRoleName.ADMIN], actor=Operator.CLI
        )
    else:
        await add_user_role(session, account.id, role=UserAccountRoleName.ADMIN, actor=Operator.CLI)
        view = await update_user_account(
            session, account.id, email=email, status=UserAccountStatus.ACTIVE, actor=Operator.CLI
        )

    purpose = (
        UserActionTokenPurpose.INVITATION
        if view.account.password_hash is None
        else UserActionTokenPurpose.PASSWORD_RESET
    )
    issued = await issue_action_token(session, settings, user_id=view.account.id, purpose=purpose, actor=Operator.CLI)
    return BootstrappedAdminAccount(account=view.account, created_account=created_account, issued=issued)
