from typing import Annotated

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.common import error_responses
from app.core.db.dependencies import get_db_session
from app.services.bot import (
    GroupMembershipNotFoundError,
    PermissionGroupNotFoundError,
    PrincipalNotFoundError,
    add_user_to_group,
    remove_user_from_group,
    upsert_discord_user,
)

from .dependencies import BotWrite
from .schemas import (
    DiscordUserResponse,
    DiscordUserUpsertRequest,
    GroupMembershipResponse,
)

router = APIRouter(prefix="/users")

DiscordId = Annotated[int, Path(ge=0)]
GroupKey = Annotated[str, Path(min_length=1)]


@router.put("/{discord_id}", response_model=DiscordUserResponse)
async def upsert_discord_user_endpoint(
    discord_id: DiscordId,
    request: DiscordUserUpsertRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _: BotWrite,
) -> DiscordUserResponse:
    """Register or update a Discord user.

    Idempotent: re-registering an existing `discord_id` updates role/nick/active instead of
    conflicting, so the bot can safely replay an onboarding event.
    """
    user = await upsert_discord_user(
        session,
        discord_id=discord_id,
        role=request.role,
        nick_name=request.nick_name,
        active=request.active,
    )
    return DiscordUserResponse.from_model(user)


@router.put(
    "/{discord_id}/groups/{group_key}",
    response_model=GroupMembershipResponse,
    responses=error_responses(PrincipalNotFoundError, PermissionGroupNotFoundError),
)
async def add_user_to_group_endpoint(
    discord_id: DiscordId,
    group_key: GroupKey,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _: BotWrite,
) -> GroupMembershipResponse:
    """Add a Discord user to a permission group. Idempotent on (user, group)."""
    membership = await add_user_to_group(session, discord_id=discord_id, group_key=group_key)
    return GroupMembershipResponse.from_model(membership)


@router.delete(
    "/{discord_id}/groups/{group_key}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(GroupMembershipNotFoundError),
)
async def remove_user_from_group_endpoint(
    discord_id: DiscordId,
    group_key: GroupKey,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _: BotWrite,
) -> None:
    """Remove a Discord user from a permission group."""
    await remove_user_from_group(session, discord_id=discord_id, group_key=group_key)
