"""Redeeming a Discord link code while its party is deleted, or while the code is redeemed again (P1-1).

Committed data, as in ``test_discord_links_concurrency.py``: every test removes its links, audit rows and party.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthSettings
from app.core.db import Database
from app.core.db.models import AuthAuditLog, DiscordAccount, Party, UserActionTokenPurpose
from app.services.auth.action_tokens import issue_action_token
from app.services.auth.audit import Operator
from app.services.auth.discord_link_codes import redeem_discord_link_code
from app.services.auth.errors import InvalidActionTokenError
from app.services.auth.users import create_user_account
from app.services.crm import parties, persons
from app.services.crm.errors import PartyInUseError
from tests.db.auth.overlap import overlapping

pytestmark = pytest.mark.db

DISCORD_ID = 123456789012345678
OTHER_ID = 987654321098765432


@dataclass(frozen=True)
class _Holder:
    """A committed person party and its active account."""

    party_id: uuid.UUID
    user_id: uuid.UUID


@pytest.fixture
async def holder(db: Database) -> AsyncIterator[_Holder]:
    """A committed person party with an active account; its links, their audit rows and the account's, and the
    party are removed afterwards."""
    async with db.session() as setup:
        party_id = (await persons.create_person(setup, firstname="Race", lastname="Code")).id
        user_id = (await create_user_account(setup, party_id=party_id, actor=Operator.CLI)).account.id
    try:
        yield _Holder(party_id=party_id, user_id=user_id)
    finally:
        async with db.session() as cleanup:
            await cleanup.execute(delete(DiscordAccount).where(DiscordAccount.party_id == party_id))
            subjects = [str(user_id), str(DISCORD_ID), str(OTHER_ID)]
            await cleanup.execute(delete(AuthAuditLog).where(AuthAuditLog.principal_id.in_(subjects)))
            if await cleanup.get(Party, party_id) is not None:
                await parties.delete_party(cleanup, party_id)


async def _link_code(db: Database, user_id: uuid.UUID, settings: AuthSettings) -> str:
    async with db.session() as setup:
        issued = await issue_action_token(
            setup, settings, user_id=user_id, purpose=UserActionTokenPurpose.DISCORD_LINK, actor=Operator.CLI
        )
    return issued.plaintext


def _redeem(code: str, discord_user_id: int) -> Callable[[AsyncSession], Coroutine[object, object, DiscordAccount]]:
    async def redeem(session: AsyncSession) -> DiscordAccount:
        return await redeem_discord_link_code(
            session, plaintext=code, discord_user_id=discord_user_id, actor=Operator.CLI
        )

    return redeem


async def test_a_redemption_waiting_for_a_party_delete_holds_nothing_the_delete_needs(
    db: Database, holder: _Holder, auth_settings: AuthSettings
):
    """``delete_party`` locks the party, then deletes the account by cascade. A redemption that locked the account
    first and then waited for the party would deadlock with it - Postgres aborts one of the two. Locking the party
    first, the redemption waits holding nothing: the delete finishes, and the redemption finds the party gone."""
    code = await _link_code(db, holder.user_id, auth_settings)
    deleter: AsyncSession = db.session_factory()
    redeemer: AsyncSession = db.session_factory()
    try:
        # delete_party's first statement, taken on its own so the redemption can start in between.
        await deleter.execute(select(Party.id).where(Party.id == holder.party_id).with_for_update())
        pending = asyncio.create_task(_redeem(code, DISCORD_ID)(redeemer))
        await asyncio.sleep(0.5)
        assert not pending.done(), "the redemption waits for the party"

        await asyncio.wait_for(parties.delete_party(deleter, holder.party_id), timeout=5)
        await deleter.commit()
        await asyncio.wait([pending], timeout=10)

        assert isinstance(pending.exception(), InvalidActionTokenError)
        # Refused at the party lock, not later when spend_action_token finds the account gone.
        assert str(pending.exception()) == "The code's account or party is gone"
        await redeemer.rollback()
    finally:
        await deleter.close()
        await redeemer.close()


async def test_a_party_delete_waiting_for_a_redemption_finds_the_new_link(
    db: Database, holder: _Holder, auth_settings: AuthSettings, session: AsyncSession
):
    code = await _link_code(db, holder.user_id, auth_settings)

    async def delete_the_party(session: AsyncSession) -> None:
        await parties.delete_party(session, holder.party_id)

    second = await overlapping(db, _redeem(code, DISCORD_ID), delete_the_party)

    assert isinstance(second.exception(), PartyInUseError)
    link = await session.get(DiscordAccount, DISCORD_ID)
    assert link is not None
    assert (link.party_id, link.active) == (holder.party_id, True)


async def test_of_two_redemptions_of_one_code_exactly_one_wins(
    db: Database, holder: _Holder, auth_settings: AuthSettings, session: AsyncSession
):
    code = await _link_code(db, holder.user_id, auth_settings)

    second = await overlapping(db, _redeem(code, DISCORD_ID), _redeem(code, OTHER_ID))

    assert isinstance(second.exception(), InvalidActionTokenError)
    links = await session.scalars(select(DiscordAccount.discord_id).where(DiscordAccount.party_id == holder.party_id))
    assert list(links) == [DISCORD_ID]
