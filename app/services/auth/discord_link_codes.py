"""Discord link codes: a person links their Discord account by typing a one-time code into the bot.

An admin issues the code for an account (``issue_action_token`` with the ``discord_link`` purpose); the bot sends it
back with the ID of the Discord user who typed it - proof of possession, never the bot's own choice (bot-decoupling
spec, decision E). The redemption lives here and not in ``discord_links``: ``accounts`` imports ``discord_links`` for
the exchange's look-up and ``action_tokens`` imports ``accounts``, so ``discord_links`` importing ``action_tokens``
would close a cycle.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import DiscordAccount, Party, UserAccount, UserAccountStatus, UserActionTokenPurpose

from .action_tokens import find_live_action_token, spend_action_token
from .audit import Actor, AuditEventType, write_user_account_audit_log
from .discord_links import link_discord_account
from .errors import InvalidActionTokenError

LINK_CODE_PURPOSES = frozenset({UserActionTokenPurpose.DISCORD_LINK})
"""The one purpose this redemption takes: an invitation or a reset never links."""


async def redeem_discord_link_code(
    session: AsyncSession, *, plaintext: str, discord_user_id: int, actor: Actor
) -> DiscordAccount:
    """Link ``discord_user_id`` to the party of the code's account and spend the code.

    The link follows ``link_discord_account``: an ID actively linked to another party is
    ``DiscordAccountAlreadyLinkedError``, and the rollback of the caller's transaction leaves the code live. Every
    refused code - unknown, used, invalidated, expired, of another purpose, its account gone or disabled, its party
    gone - is one ``InvalidActionTokenError``.

    The party is locked before the account, in the order of ``delete_party`` (the party, then its rows by cascade):
    a redemption racing the party's deletion waits holding nothing the delete needs, and then finds the party gone.
    Locked the other way round, each would wait for the other.
    """
    found = await find_live_action_token(session, plaintext, purposes=LINK_CODE_PURPOSES)
    party_id = await session.scalar(select(UserAccount.party_id).where(UserAccount.id == found.user_account_id))
    if party_id is None or not await _lock_party(session, party_id):
        raise InvalidActionTokenError("The code's account or party is gone")
    account = await spend_action_token(session, found)
    if account.status is not UserAccountStatus.ACTIVE:
        raise InvalidActionTokenError("The code's account is disabled")

    link = await link_discord_account(session, discord_user_id=discord_user_id, party_id=party_id, actor=actor)
    await write_user_account_audit_log(
        session,
        account.id,
        AuditEventType.DISCORD_LINK_CODE_REDEEMED,
        f"Redeemed a Discord link code for Discord user {discord_user_id}",
        actor=actor,
    )
    return link


async def _lock_party(session: AsyncSession, party_id: uuid.UUID) -> bool:
    """Lock the party the way ``link_discord_account`` will (``FOR NO KEY UPDATE``); ``False`` once it is gone."""
    locked = await session.scalar(select(Party.id).where(Party.id == party_id).with_for_update(key_share=True))
    return locked is not None
