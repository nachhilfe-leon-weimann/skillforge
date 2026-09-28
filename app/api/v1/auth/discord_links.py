"""Discord links: which Discord account speaks for which person party (bot-decoupling spec, P0-2 and P1-1).

Reads need `auth:discord-links:read`, writes `auth:users:manage`: a link is a login credential once the bot
exchanges Discord users for tokens. The one write without an admin is the redemption of a one-time link code, which
the bot sends for the person who typed it (`auth:users:exchange`).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status
from pydantic import Field

from app.api.v1.common import DBSession, Page, PageParams, UpdatedSince, error_responses
from app.core.auth import Scope
from app.core.auth.dependencies import require_scopes
from app.services.auth import discord_link_codes as discord_link_codes_service
from app.services.auth import discord_links as discord_links_service
from app.services.auth.errors import (
    DiscordAccountAlreadyLinkedError,
    DiscordLinkNotFoundError,
    InvalidActionTokenError,
    LinkPartyNotAPersonError,
    UnknownLinkPartyError,
)

from .params import DiscordUserIdPath, ExchangeClient, ManageUsers
from .schemas import DiscordLink, DiscordLinkRedeemRequest, DiscordLinkRequest

router = APIRouter(prefix="/discord-links")


class DiscordLinkListParams(PageParams):
    """Filters of `GET /discord-links`. Unlinked rows are included, so a feed sees deactivations."""

    updated_since: UpdatedSince = None
    party_id: UUID | None = Field(None, description="Only the links of this person party.")
    active: bool | None = Field(None, description="Only active (`true`) or only unlinked (`false`) links.")


type DiscordLinkListQuery = Annotated[DiscordLinkListParams, Query()]


@router.get("", dependencies=[require_scopes(Scope.AUTH_DISCORD_LINKS_READ)])
async def list_discord_links(params: DiscordLinkListQuery, session: DBSession) -> Page[DiscordLink]:
    """List Discord links ordered by Discord user ID, unlinked ones included: a change feed (ADR 0009)."""
    links, total = await discord_links_service.list_discord_links(session, **params.model_dump())
    return Page.of([DiscordLink.from_model(link) for link in links], total=total, params=params)


@router.get(
    "/{discord_user_id}",
    dependencies=[require_scopes(Scope.AUTH_DISCORD_LINKS_READ)],
    responses=error_responses(DiscordLinkNotFoundError),
)
async def get_discord_link(discord_user_id: DiscordUserIdPath, session: DBSession) -> DiscordLink:
    """Read the link of one Discord user, active or not."""
    return DiscordLink.from_model(await discord_links_service.get_discord_link(session, discord_user_id))


@router.put(
    "/{discord_user_id}",
    responses=error_responses(UnknownLinkPartyError, LinkPartyNotAPersonError, DiscordAccountAlreadyLinkedError),
)
async def link_discord_account(
    discord_user_id: DiscordUserIdPath, request: DiscordLinkRequest, session: DBSession, principal: ManageUsers
) -> DiscordLink:
    """Let a Discord user speak for a person party. Idempotent.

    An unlinked Discord user is linked again, or moved when it named another party; one linked to another party
    is refused - unlink it there first.
    """
    link = await discord_links_service.link_discord_account(
        session, discord_user_id=discord_user_id, party_id=request.party_id, actor=principal
    )
    return DiscordLink.from_model(link)


@router.delete(
    "/{discord_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(DiscordLinkNotFoundError),
)
async def unlink_discord_account(
    discord_user_id: DiscordUserIdPath, session: DBSession, principal: ManageUsers
) -> None:
    """Unlink a Discord user; the row stays, inactive, in the feed. Unlinking an unlinked one changes nothing."""
    await discord_links_service.unlink_discord_account(session, discord_user_id=discord_user_id, actor=principal)


@router.post("/redeem", responses=error_responses(InvalidActionTokenError, DiscordAccountAlreadyLinkedError))
async def redeem_discord_link_code(
    request: DiscordLinkRedeemRequest, session: DBSession, principal: ExchangeClient
) -> DiscordLink:
    """Link the Discord user who sent a one-time link code to the party of the code's account; spends the code.

    The bot's route: `auth:users:exchange` in `application` mode, and a person's token is refused whatever it
    carries. Send the Discord user who typed the code, never an ID from a command option. An unknown, used,
    invalidated or expired code, and one whose account is disabled or gone, answer one and the same body. A Discord
    user linked to another party is refused, and the code stays live.
    """
    link = await discord_link_codes_service.redeem_discord_link_code(
        session, plaintext=request.token, discord_user_id=request.discord_user_id, actor=principal
    )
    return DiscordLink.from_model(link)
