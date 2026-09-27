from collections.abc import Iterable, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.scopes import CLIENT_ONLY_SCOPES, Scope, canonical, expand, parse_scopes
from app.core.db.models import ApplicationClient, ApplicationClientScopeGrant, GrantMode, PermissionScope

from .audit import AuditEventType, write_auth_audit_log
from .clients import get_application_client
from .errors import ApplicationClientScopeGrantNotFoundError, InvalidClientScopeError

EXCLUSIVE_CLIENT_SCOPES: frozenset[Scope] = frozenset({Scope.AUTH_USERS_LOGIN, Scope.AUTH_USERS_EXCHANGE})
"""A client never holds both of these (bot-decoupling spec, decision T): the portal can never vouch for a person,
the bot can never take a password - by code, not by discipline."""


async def seed_default_scopes(session: AsyncSession) -> list[PermissionScope]:
    scopes: list[PermissionScope] = []
    for scope in Scope:
        permission_scope = await session.get(PermissionScope, scope.value)
        if permission_scope is None:
            permission_scope = PermissionScope(
                key=scope.value,
                description=scope.description,
                active=True,
            )
            session.add(permission_scope)
        else:
            permission_scope.description = scope.description
            permission_scope.active = True

        scopes.append(permission_scope)

    await session.flush()
    return scopes


async def grant_application_client_scopes(
    session: AsyncSession,
    *,
    client_id: str,
    scopes: Iterable[Scope | str],
    mode: GrantMode = GrantMode.APPLICATION,
) -> ApplicationClient:
    await seed_default_scopes(session)
    client = await get_application_client(session, client_id=client_id)
    scope_keys = parse_scopes(scopes)
    await grant_client_scopes(session, client=client, scope_keys=scope_keys, mode=mode)
    return await get_application_client(session, client_id=client.client_id)


async def revoke_application_client_scope(
    session: AsyncSession,
    *,
    client_id: str,
    scope_key: str,
    mode: GrantMode = GrantMode.APPLICATION,
) -> None:
    client = await get_application_client(session, client_id=client_id)
    grant = await session.get(ApplicationClientScopeGrant, (client.id, scope_key, mode))
    if grant is None:
        raise ApplicationClientScopeGrantNotFoundError("Application client scope grant not found")

    await session.delete(grant)
    await session.flush()
    await write_auth_audit_log(
        session,
        principal_type="application",
        principal_id=client.id,
        event_type=AuditEventType.SCOPE_GRANT_REMOVED,
        success=True,
        detail=f"Removed scope {scope_key} in {mode} mode from application client {client.client_id}.",
    )


def granted_active_scope_keys(
    scope_grants: Iterable[ApplicationClientScopeGrant], *, mode: GrantMode
) -> frozenset[str]:
    """Return the scopes of ``scope_grants`` granted in ``mode`` whose ``PermissionScope`` is active."""
    return frozenset(
        grant.scope_key
        for grant in scope_grants
        if grant.mode == mode and grant.permission_scope is not None and grant.permission_scope.active
    )


async def grant_client_scopes(
    session: AsyncSession,
    *,
    client: ApplicationClient,
    scope_keys: frozenset[str],
    mode: GrantMode,
) -> None:
    """Grant ``scope_keys`` to ``client`` in ``mode``; a scope already granted in that mode is kept.

    Checks the whole request before it writes a grant, so a refused request grants nothing: every
    scope must be known and active, a client-only scope is refused in ``delegated`` mode
    (ADR 0008), and no client ends up holding both ``EXCLUSIVE_CLIENT_SCOPES``.
    """
    if mode == GrantMode.DELEGATED and not CLIENT_ONLY_SCOPES.isdisjoint(scope_keys):
        raise InvalidClientScopeError("Client-only scopes cannot be granted in delegated mode")

    permission_scopes = [await _active_permission_scope(session, scope_key) for scope_key in sorted(scope_keys)]
    # Lock the client row before reading its grants: decision T's check-then-write needs them stable, or two
    # concurrent grants on the same client (e.g. one `auth:users:login`, one `auth:users:exchange`) could both pass.
    await session.execute(select(ApplicationClient.id).where(ApplicationClient.id == client.id).with_for_update())
    existing_scope_keys = set(
        (
            await session.execute(
                select(ApplicationClientScopeGrant.scope_key).where(
                    ApplicationClientScopeGrant.application_client_id == client.id,
                    ApplicationClientScopeGrant.mode == mode,
                )
            )
        )
        .scalars()
        .all()
    )
    if EXCLUSIVE_CLIENT_SCOPES <= existing_scope_keys | scope_keys:
        raise InvalidClientScopeError("A client cannot both log people in and exchange Discord users")

    for permission_scope in permission_scopes:
        if permission_scope.key in existing_scope_keys:
            continue

        session.add(
            ApplicationClientScopeGrant(application_client=client, permission_scope=permission_scope, mode=mode)
        )
        await write_auth_audit_log(
            session,
            principal_type="application",
            principal_id=client.id,
            event_type=AuditEventType.SCOPE_GRANT_ADDED,
            success=True,
            detail=f"Granted scope {permission_scope.key} in {mode} mode to application client {client.client_id}.",
        )

    await session.flush()


def resolve_token_scopes(
    *,
    requested: Set[str],
    granted: Set[str],
    ceilings: Iterable[Set[str]] = (),
) -> frozenset[str]:
    """Compute the canonical scopes of a token to be issued (ADR 0008).

    ``granted`` are the client's grants of the mode that applies; each of ``ceilings`` narrows them
    (a person's role scopes and, on refresh, the session's scope). Every set is expanded first, so
    a grant of ``crm:read`` makes ``crm:read:own`` available too. An empty ``requested`` means none
    was requested and the token gets everything available; otherwise every requested scope must be
    available, and the token carries exactly those. An empty result is ``invalid_scope``.
    """
    available = expand(granted).intersection(*(expand(ceiling) for ceiling in ceilings))
    if not requested <= available:
        if requested <= expand(granted):
            raise InvalidClientScopeError("Requested scopes exceed the ceiling")

        raise InvalidClientScopeError("Requested scopes are not granted")

    token_scopes = canonical(requested or available)
    if not token_scopes:
        if granted:
            raise InvalidClientScopeError("Client grants and ceilings have no scope in common")

        raise InvalidClientScopeError("Client has no active scope grants")

    return token_scopes


async def _active_permission_scope(session: AsyncSession, scope_key: str) -> PermissionScope:
    permission_scope = await session.get(PermissionScope, scope_key)
    if permission_scope is None or not permission_scope.active:
        raise InvalidClientScopeError("Requested scopes are not known or active")

    return permission_scope
