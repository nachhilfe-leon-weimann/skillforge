"""Link writes racing a party delete or each other (bot-decoupling P0-2, decisions G and H).

Committed data, as in ``test_auth_users_concurrency.py``: every test removes its links, audit rows and parties.
"""

import asyncio
import uuid
from collections import Counter
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Database
from app.core.db.models import AuthAuditLog, DiscordAccount, Party
from app.services.auth import discord_links
from app.services.auth.audit import Operator
from app.services.auth.discord_links import link_discord_account, unlink_discord_account
from app.services.auth.errors import DiscordAccountAlreadyLinkedError, UnknownLinkPartyError
from app.services.crm import parties, persons
from app.services.crm.errors import PartyInUseError
from tests.db.auth.overlap import overlapping

pytestmark = pytest.mark.db

DISCORD_ID = 123456789012345678


@pytest.fixture
async def party_ids(db: Database) -> AsyncIterator[list[uuid.UUID]]:
    """Two committed person parties; their links, the links' audit rows and the parties are removed afterwards."""
    async with db.session() as setup:
        ids = [(await persons.create_person(setup, firstname=name, lastname="Race")).id for name in ("Anna", "Ben")]
    try:
        yield ids
    finally:
        async with db.session() as cleanup:
            await cleanup.execute(delete(DiscordAccount).where(DiscordAccount.party_id.in_(ids)))
            await cleanup.execute(delete(AuthAuditLog).where(AuthAuditLog.principal_id == str(DISCORD_ID)))
            for party_id in ids:
                if await cleanup.get(Party, party_id) is not None:
                    await parties.delete_party(cleanup, party_id)


async def _unlinked(db: Database, party_id: uuid.UUID) -> None:
    async with db.session() as setup:
        await link_discord_account(setup, discord_user_id=DISCORD_ID, party_id=party_id, actor=Operator.CLI)
        await unlink_discord_account(setup, discord_user_id=DISCORD_ID, actor=Operator.CLI)


async def _link_events(session: AsyncSession, discord_user_id: int) -> Counter[str]:
    rows = await session.scalars(
        select(AuthAuditLog.event_type).where(AuthAuditLog.principal_id == str(discord_user_id))
    )
    return Counter(rows)


async def test_a_relink_waiting_on_a_party_delete_finds_no_party(db: Database, party_ids: list[uuid.UUID]):
    await _unlinked(db, party_ids[0])

    async def delete_party(session: AsyncSession) -> None:
        await parties.delete_party(session, party_ids[0])

    async def relink(session: AsyncSession) -> DiscordAccount:
        return await link_discord_account(
            session, discord_user_id=DISCORD_ID, party_id=party_ids[0], actor=Operator.CLI
        )

    second = await overlapping(db, delete_party, relink)

    assert isinstance(second.exception(), UnknownLinkPartyError)


async def test_a_party_delete_waiting_on_a_relink_finds_it_in_use(db: Database, party_ids: list[uuid.UUID]):
    await _unlinked(db, party_ids[0])

    async def relink(session: AsyncSession) -> DiscordAccount:
        return await link_discord_account(
            session, discord_user_id=DISCORD_ID, party_id=party_ids[0], actor=Operator.CLI
        )

    async def delete_party(session: AsyncSession) -> None:
        await parties.delete_party(session, party_ids[0])

    second = await overlapping(db, relink, delete_party)

    assert isinstance(second.exception(), PartyInUseError)


async def test_two_links_of_one_id_for_two_parties_leave_one(
    db: Database, party_ids: list[uuid.UUID], session: AsyncSession
):
    def link_to(party_id: uuid.UUID):
        async def _link(session: AsyncSession) -> DiscordAccount:
            return await link_discord_account(
                session, discord_user_id=DISCORD_ID, party_id=party_id, actor=Operator.CLI
            )

        return _link

    second = await overlapping(db, link_to(party_ids[0]), link_to(party_ids[1]))

    assert isinstance(second.exception(), DiscordAccountAlreadyLinkedError)
    assert (
        await session.scalar(select(DiscordAccount.party_id).where(DiscordAccount.discord_id == DISCORD_ID))
        == party_ids[0]
    )


async def test_a_link_whose_inactive_row_vanishes_mid_write_is_added_anew(
    db: Database, party_ids: list[uuid.UUID], session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """The retry loop's reason to exist (Task 4 review): X is inactive on A; a link X -> B inserts nothing (X is
    still there as a row), meanwhile ``delete_party(A)`` is not blocked by the inactive link, deletes A and
    cascades X away, and commits before the link's row lock runs. The lock then finds nothing, the loop retries
    the insert, and this time it succeeds - a fresh, active link on B, audited as ``discord_link.added``."""
    await _unlinked(db, party_ids[0])
    before = await _link_events(session, DISCORD_ID)

    original_lock_link = discord_links._lock_link
    calls = 0

    async def lock_link_first_call_deletes_party_a(
        session: AsyncSession, discord_user_id: int
    ) -> DiscordAccount | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            async with db.session() as delete_session:
                await asyncio.wait_for(parties.delete_party(delete_session, party_ids[0]), timeout=10)
        return await original_lock_link(session, discord_user_id)

    monkeypatch.setattr(discord_links, "_lock_link", lock_link_first_call_deletes_party_a)

    async with db.session() as link_session:
        link = await link_discord_account(
            link_session, discord_user_id=DISCORD_ID, party_id=party_ids[1], actor=Operator.CLI
        )

    assert (link.party_id, link.active) == (party_ids[1], True)
    assert calls == 2, "the loop retries exactly once after the lock found nothing"
    assert await session.get(Party, party_ids[0]) is None

    reread = await discord_links.get_discord_link(session, DISCORD_ID)
    assert (reread.party_id, reread.active) == (party_ids[1], True)

    after = await _link_events(session, DISCORD_ID)
    assert after == before + Counter({"discord_link.added": 1})
