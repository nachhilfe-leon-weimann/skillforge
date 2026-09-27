from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import (
    DiscordUser,
    DiscordUserPermissionGroup,
    MemberRole,
    PermissionGroup,
)

from .errors import (
    GroupMembershipNotFoundError,
    PermissionGroupNotFoundError,
    PrincipalNotFoundError,
)


async def upsert_discord_user(
    session: AsyncSession,
    *,
    discord_id: int,
    role: MemberRole,
    nick_name: str,
    active: bool = True,
) -> DiscordUser:
    """Create or update the Discord user identified by ``discord_id``.

    Idempotent: re-registering an existing user updates its mutable fields instead of conflicting, so
    the bot can safely replay an onboarding event after an at-least-once retry.
    """
    user = await session.get(DiscordUser, discord_id)
    if user is None:
        user = DiscordUser(discord_id=discord_id, role=role, nick_name=nick_name, active=active)
        session.add(user)
    else:
        user.role = role
        user.nick_name = nick_name
        user.active = active

    await session.flush()
    return user


async def add_user_to_group(session: AsyncSession, *, discord_id: int, group_key: str) -> DiscordUserPermissionGroup:
    """Add a Discord user to a permission group. Idempotent: an existing membership is returned as-is.

    Validates both the user and the group up front so the common errors surface cleanly
    (:class:`PrincipalNotFoundError` / :class:`PermissionGroupNotFoundError`) instead of as a foreign
    key violation.
    """
    if await session.get(DiscordUser, discord_id) is None:
        raise PrincipalNotFoundError("Discord principal not found")
    if await session.get(PermissionGroup, group_key) is None:
        raise PermissionGroupNotFoundError("Permission group not found")

    membership = await session.get(DiscordUserPermissionGroup, {"discord_id": discord_id, "group_key": group_key})
    if membership is None:
        membership = DiscordUserPermissionGroup(discord_id=discord_id, group_key=group_key)
        session.add(membership)
        await session.flush()

    return membership


async def remove_user_from_group(session: AsyncSession, *, discord_id: int, group_key: str) -> None:
    """Remove a Discord user from a permission group.

    Raises :class:`GroupMembershipNotFoundError` if the user is not a member of the group.
    """
    membership = await session.get(DiscordUserPermissionGroup, {"discord_id": discord_id, "group_key": group_key})
    if membership is None:
        raise GroupMembershipNotFoundError("Group membership not found")

    await session.delete(membership)
    await session.flush()
