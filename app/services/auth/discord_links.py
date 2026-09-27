"""Discord links: which Discord account speaks for which person party (bot-decoupling spec, decisions D to H).

A link is identity, not CRM data: it lives in ``ext.discord_account`` beside the party, and this module is its only
writer. Once the token exchange exists a link is a login credential, so every change is audited and only an admin
(``auth:users:manage``) writes one. This module imports models, ``audit`` and ``errors`` only; the exchange and the
link code build on it, never the other way round.
"""

import uuid
from datetime import datetime

from sqlalchemy import ScalarSelect, func, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import DiscordAccount, Party, PartyType

from .audit import Actor, AuditEventType, write_discord_link_audit_log
from .errors import (
    DiscordAccountAlreadyLinkedError,
    DiscordLinkNotFoundError,
    LinkPartyNotAPersonError,
    UnknownLinkPartyError,
)


def active_link_party_id(discord_user_id: int) -> ScalarSelect[uuid.UUID]:
    """The party an active link of ``discord_user_id`` names, as a scalar subquery: the one home of "active link"."""
    return (
        select(DiscordAccount.party_id)
        .where(DiscordAccount.discord_id == discord_user_id, DiscordAccount.active.is_(true()))
        .scalar_subquery()
    )


async def get_discord_link(session: AsyncSession, discord_user_id: int) -> DiscordAccount:
    """Return the link of ``discord_user_id``, active or not, as the database says now.

    ``populate_existing`` reloads a copy the session already holds - reading it outside a lock could otherwise
    raise ``MissingGreenlet``.
    """
    link = await session.get(DiscordAccount, discord_user_id, populate_existing=True)
    if link is None:
        raise DiscordLinkNotFoundError(f"No link for Discord user {discord_user_id}")
    return link


async def list_discord_links(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    updated_since: datetime | None = None,
    party_id: uuid.UUID | None = None,
    active: bool | None = None,
) -> tuple[list[DiscordAccount], int]:
    """Return one page of links ordered by Discord user ID, unlinked ones included, plus the total count."""
    conditions = []
    if updated_since is not None:
        conditions.append(DiscordAccount.updated_at >= updated_since)
    if party_id is not None:
        conditions.append(DiscordAccount.party_id == party_id)
    if active is not None:
        conditions.append(DiscordAccount.active.is_(active))

    total = (await session.execute(select(func.count()).select_from(DiscordAccount).where(*conditions))).scalar_one()
    links = await session.scalars(
        select(DiscordAccount).where(*conditions).order_by(DiscordAccount.discord_id).limit(limit).offset(offset)
    )
    return list(links), total


async def link_discord_account(
    session: AsyncSession, *, discord_user_id: int, party_id: uuid.UUID, actor: Actor
) -> DiscordAccount:
    """Let ``discord_user_id`` speak for the person party ``party_id``; idempotent.

    A new ID is linked; an unlinked one is linked again, or moved when it named another party; an ID actively
    linked to another party is refused - unlink it there first. No write moves the party's ``updated_at``: the CRM
    knows nothing about Discord, the link feed is the signal.
    """
    await _lock_person_party(session, party_id)
    # ``ON CONFLICT DO NOTHING`` takes no lock on a conflicting row, so it can vanish (a concurrent
    # ``delete_party`` cascades through it) between the INSERT and the row lock below. Retrying the INSERT
    # then finds no conflict and creates our own row, which we hold locked under this party - it terminates
    # in at most one extra pass.
    while True:
        inserted = await session.scalar(
            insert(DiscordAccount)
            .values(discord_id=discord_user_id, party_id=party_id, active=True, is_primary=False)
            .on_conflict_do_nothing(index_elements=[DiscordAccount.discord_id])
            .returning(DiscordAccount.discord_id)
        )
        link = await _lock_link(session, discord_user_id)
        if link is not None:
            break

    if inserted is not None:
        event, what = AuditEventType.DISCORD_LINK_ADDED, f"Linked Discord user {discord_user_id} to party {party_id}"
    elif link.active and link.party_id == party_id:
        return link
    elif link.active:
        raise DiscordAccountAlreadyLinkedError(f"Discord user {discord_user_id} is linked to another party")
    elif link.party_id == party_id:
        event = AuditEventType.DISCORD_LINK_REACTIVATED
        what = f"Linked Discord user {discord_user_id} to party {party_id} again"
    else:
        event = AuditEventType.DISCORD_LINK_MOVED
        what = f"Moved Discord user {discord_user_id} from party {link.party_id} to party {party_id}"

    if inserted is None:
        link.party_id, link.active, link.is_primary = party_id, True, False
        await session.flush()
        await session.refresh(link)
    await write_discord_link_audit_log(session, discord_user_id, event, what, actor=actor)
    return link


async def unlink_discord_account(session: AsyncSession, *, discord_user_id: int, actor: Actor) -> None:
    """Unlink ``discord_user_id``: the row stays, inactive, as history. Unlinking an unlinked ID records nothing."""
    link = await _lock_link(session, discord_user_id)
    if link is None:
        raise DiscordLinkNotFoundError(f"No link for Discord user {discord_user_id}")
    if not link.active:
        return

    link.active, link.is_primary = False, False
    await session.flush()
    await write_discord_link_audit_log(
        session,
        discord_user_id,
        AuditEventType.DISCORD_LINK_REMOVED,
        f"Unlinked Discord user {discord_user_id} from party {link.party_id}",
        actor=actor,
    )


async def _lock_person_party(session: AsyncSession, party_id: uuid.UUID) -> None:
    """Lock the party a link will name, as ``create_user_account`` does.

    Two reasons: link writes to one party are serialized, and ``delete_party`` waits for them - a re-activation
    moves no foreign key, so without this lock it could come back to life in the gap before the party is deleted.
    """
    party_type = await session.scalar(select(Party.type).where(Party.id == party_id).with_for_update(key_share=True))
    if party_type is None:
        raise UnknownLinkPartyError(f"No party with id {party_id}")
    if party_type is not PartyType.PERSON:
        raise LinkPartyNotAPersonError(f"Party {party_id} is a company")


async def _lock_link(session: AsyncSession, discord_user_id: int) -> DiscordAccount | None:
    return await session.scalar(
        select(DiscordAccount)
        .where(DiscordAccount.discord_id == discord_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
