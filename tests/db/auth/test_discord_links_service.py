"""The Discord link service: the one writer of ``ext.discord_account`` (bot-decoupling spec, P0-2).

Ports the link tests of ``tests/db/test_bot_provisioning_service.py`` and adds the rules of decisions D to H.
"""

import uuid
from collections import Counter
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import AuthAuditLog, DiscordAccount, Party
from app.services.auth.audit import AuditSubjectType, Operator
from app.services.auth.discord_links import (
    get_discord_link,
    link_discord_account,
    list_discord_links,
    unlink_discord_account,
)
from app.services.auth.errors import (
    DiscordAccountAlreadyLinkedError,
    DiscordLinkNotFoundError,
    LinkPartyNotAPersonError,
    UnknownLinkPartyError,
)

pytestmark = pytest.mark.db

# Snowflake-sized IDs (> 2**53): the tests fail if an ID passes through a float or a 32-bit integer.
DISCORD_ID = 123456789012345678
OTHER_ID = 987654321098765432
LONG_AGO = datetime(2001, 1, 1, tzinfo=UTC)
LATER = datetime(2002, 1, 1, tzinfo=UTC)
ACTOR = Operator.CLI


async def _events(session: AsyncSession, discord_user_id: int) -> Counter[str]:
    rows = await session.scalars(
        select(AuthAuditLog.event_type).where(
            AuthAuditLog.principal_type == AuditSubjectType.DISCORD_USER,
            AuthAuditLog.principal_id == str(discord_user_id),
        )
    )
    return Counter(rows)


async def _details(session: AsyncSession, discord_user_id: int) -> list[str]:
    rows = await session.scalars(select(AuthAuditLog.detail).where(AuthAuditLog.principal_id == str(discord_user_id)))
    return [detail or "" for detail in rows]


async def _stamp(session: AsyncSession, discord_user_id: int, when: datetime) -> None:
    await session.execute(
        update(DiscordAccount).where(DiscordAccount.discord_id == discord_user_id).values(updated_at=when)
    )


async def test_linking_needs_an_existing_party(session):
    with pytest.raises(UnknownLinkPartyError):
        await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=uuid.uuid4(), actor=ACTOR)


async def test_linking_refuses_a_company(session, make_company):
    company = await make_company()

    with pytest.raises(LinkPartyNotAPersonError):
        await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=company.id, actor=ACTOR)


async def test_linking_a_person_creates_an_active_link_and_records_it(session, make_person):
    person = await make_person()

    link = await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=person.id, actor=ACTOR)

    assert (link.discord_id, link.party_id, link.active, link.is_primary) == (DISCORD_ID, person.id, True, False)
    assert link.created_at is not None and link.updated_at is not None
    assert await _events(session, DISCORD_ID) == Counter({"discord_link.added": 1})
    assert await _details(session, DISCORD_ID) == [f"Linked Discord user {DISCORD_ID} to party {person.id} by cli."]


async def test_linking_again_to_the_same_party_changes_nothing(session, make_person):
    person = await make_person()
    await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=person.id, actor=ACTOR)
    await _stamp(session, DISCORD_ID, LONG_AGO)

    link = await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=person.id, actor=ACTOR)

    assert link.updated_at == LONG_AGO
    assert await _events(session, DISCORD_ID) == Counter({"discord_link.added": 1})


async def test_a_person_may_hold_several_active_links(session, make_person):
    person = await make_person()

    await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=person.id, actor=ACTOR)
    await link_discord_account(session, discord_user_id=OTHER_ID, party_id=person.id, actor=ACTOR)

    links, total = await list_discord_links(session, limit=10, offset=0, party_id=person.id, active=True)
    assert total == 2
    assert [link.discord_id for link in links] == [DISCORD_ID, OTHER_ID]


async def test_linking_an_id_active_elsewhere_is_a_conflict_and_changes_nothing(session, make_person):
    first, second = await make_person("Anna"), await make_person("Ben")
    await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=first.id, actor=ACTOR)

    with pytest.raises(DiscordAccountAlreadyLinkedError):
        await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=second.id, actor=ACTOR)

    link = await get_discord_link(session, DISCORD_ID)
    assert (link.party_id, link.active) == (first.id, True)
    assert await _events(session, DISCORD_ID) == Counter({"discord_link.added": 1})


async def test_linking_an_unlinked_id_to_its_party_again_reactivates_it(session, make_person):
    person = await make_person()
    await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=person.id, actor=ACTOR)
    await unlink_discord_account(session, discord_user_id=DISCORD_ID, actor=ACTOR)

    link = await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=person.id, actor=ACTOR)

    assert link.active is True
    assert await _events(session, DISCORD_ID) == Counter({
        "discord_link.added": 1,
        "discord_link.removed": 1,
        "discord_link.reactivated": 1,
    })


async def test_linking_an_unlinked_id_to_another_party_moves_it_and_names_both(session, make_person):
    first, second = await make_person("Anna"), await make_person("Ben")
    created = await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=first.id, actor=ACTOR)
    created_at = created.created_at
    await unlink_discord_account(session, discord_user_id=DISCORD_ID, actor=ACTOR)

    link = await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=second.id, actor=ACTOR)

    assert (link.party_id, link.active, link.created_at) == (second.id, True, created_at)
    moved = [detail for detail in await _details(session, DISCORD_ID) if detail.startswith("Moved")]
    assert moved == [f"Moved Discord user {DISCORD_ID} from party {first.id} to party {second.id} by cli."]


@pytest.mark.parametrize("scenario", ["reactivate", "move", "unlink"])
async def test_every_write_leaves_is_primary_false(session, make_person, scenario):
    first, second = await make_person("Anna"), await make_person("Ben")

    match scenario:
        case "reactivate":
            session.add(DiscordAccount(discord_id=DISCORD_ID, party_id=first.id, active=False, is_primary=True))
            await session.flush()
            await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=first.id, actor=ACTOR)
        case "move":
            session.add(DiscordAccount(discord_id=DISCORD_ID, party_id=first.id, active=False, is_primary=True))
            await session.flush()
            await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=second.id, actor=ACTOR)
        case "unlink":
            session.add(DiscordAccount(discord_id=DISCORD_ID, party_id=first.id, active=True, is_primary=True))
            await session.flush()
            await unlink_discord_account(session, discord_user_id=DISCORD_ID, actor=ACTOR)

    link = await get_discord_link(session, DISCORD_ID)
    assert link.is_primary is False


async def test_unlinking_keeps_the_row_inactive_and_records_it(session, make_person):
    person = await make_person()
    await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=person.id, actor=ACTOR)

    await unlink_discord_account(session, discord_user_id=DISCORD_ID, actor=ACTOR)

    link = await get_discord_link(session, DISCORD_ID)
    assert (link.active, link.is_primary, link.party_id) == (False, False, person.id)
    assert (await _events(session, DISCORD_ID))["discord_link.removed"] == 1


async def test_unlinking_an_unlinked_id_records_nothing(session, make_person):
    person = await make_person()
    await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=person.id, actor=ACTOR)
    await unlink_discord_account(session, discord_user_id=DISCORD_ID, actor=ACTOR)

    await unlink_discord_account(session, discord_user_id=DISCORD_ID, actor=ACTOR)

    assert (await _events(session, DISCORD_ID))["discord_link.removed"] == 1


async def test_unlinking_or_reading_an_unknown_id_is_not_found(session):
    with pytest.raises(DiscordLinkNotFoundError):
        await unlink_discord_account(session, discord_user_id=DISCORD_ID, actor=ACTOR)
    with pytest.raises(DiscordLinkNotFoundError):
        await get_discord_link(session, DISCORD_ID)


async def test_reading_after_an_unlink_in_the_same_session_shows_the_new_updated_at(session, make_person):
    # ``created`` is kept alive on purpose: the session's identity map only weakly references a row, and without
    # a live reference to it, ``get_discord_link`` would just load a fresh copy - which never shows the bug this
    # guards (see ``get_discord_link``'s ``populate_existing``: a row the session already holds must be reloaded).
    person = await make_person()
    created = await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=person.id, actor=ACTOR)
    assert created.party_id == person.id
    await _stamp(session, DISCORD_ID, LONG_AGO)

    await unlink_discord_account(session, discord_user_id=DISCORD_ID, actor=ACTOR)

    assert (await get_discord_link(session, DISCORD_ID)).updated_at > LONG_AGO


async def test_reading_after_a_reactivation_in_the_same_session_shows_the_new_updated_at(session, make_person):
    person = await make_person()
    created = await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=person.id, actor=ACTOR)
    assert created.party_id == person.id
    await unlink_discord_account(session, discord_user_id=DISCORD_ID, actor=ACTOR)
    await _stamp(session, DISCORD_ID, LONG_AGO)

    await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=person.id, actor=ACTOR)

    assert (await get_discord_link(session, DISCORD_ID)).updated_at > LONG_AGO


async def test_reading_after_a_move_in_the_same_session_shows_the_new_updated_at(session, make_person):
    first, second = await make_person("Anna"), await make_person("Ben")
    created = await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=first.id, actor=ACTOR)
    assert created.party_id == first.id
    await unlink_discord_account(session, discord_user_id=DISCORD_ID, actor=ACTOR)
    await _stamp(session, DISCORD_ID, LONG_AGO)

    await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=second.id, actor=ACTOR)

    assert (await get_discord_link(session, DISCORD_ID)).updated_at > LONG_AGO


async def test_the_list_orders_by_discord_id_and_includes_unlinked_rows(session, make_person):
    person = await make_person()
    await link_discord_account(session, discord_user_id=OTHER_ID, party_id=person.id, actor=ACTOR)
    await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=person.id, actor=ACTOR)
    await unlink_discord_account(session, discord_user_id=OTHER_ID, actor=ACTOR)

    links, total = await list_discord_links(session, limit=10, offset=0, party_id=person.id)

    assert total == 2
    assert [(link.discord_id, link.active) for link in links] == [(DISCORD_ID, True), (OTHER_ID, False)]


async def test_the_list_keeps_the_updated_since_boundary(session, make_person):
    person = await make_person()
    await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=person.id, actor=ACTOR)
    await link_discord_account(session, discord_user_id=OTHER_ID, party_id=person.id, actor=ACTOR)
    await _stamp(session, DISCORD_ID, LONG_AGO)
    await _stamp(session, OTHER_ID, LATER)

    links, total = await list_discord_links(session, limit=10, offset=0, updated_since=LATER, party_id=person.id)

    assert (total, [link.discord_id for link in links]) == (1, [OTHER_ID])


async def test_the_list_filters_by_active(session, make_person):
    person = await make_person()
    await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=person.id, actor=ACTOR)
    await link_discord_account(session, discord_user_id=OTHER_ID, party_id=person.id, actor=ACTOR)
    await unlink_discord_account(session, discord_user_id=OTHER_ID, actor=ACTOR)

    active, _ = await list_discord_links(session, limit=10, offset=0, party_id=person.id, active=True)
    unlinked, _ = await list_discord_links(session, limit=10, offset=0, party_id=person.id, active=False)

    assert [link.discord_id for link in active] == [DISCORD_ID]
    assert [link.discord_id for link in unlinked] == [OTHER_ID]


async def test_a_deactivated_link_stays_in_the_feed_with_its_newer_updated_at(session, make_person):
    person = await make_person()
    await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=person.id, actor=ACTOR)
    await _stamp(session, DISCORD_ID, LONG_AGO)

    await unlink_discord_account(session, discord_user_id=DISCORD_ID, actor=ACTOR)

    links, total = await list_discord_links(session, limit=10, offset=0, updated_since=LATER)
    assert total == 1
    assert (links[0].discord_id, links[0].active) == (DISCORD_ID, False)


async def test_no_link_write_moves_the_party(session, make_person):
    person = await make_person()
    await session.execute(update(Party).where(Party.id == person.id).values(updated_at=LONG_AGO))

    await link_discord_account(session, discord_user_id=DISCORD_ID, party_id=person.id, actor=ACTOR)
    await unlink_discord_account(session, discord_user_id=DISCORD_ID, actor=ACTOR)

    assert await session.scalar(select(Party.updated_at).where(Party.id == person.id)) == LONG_AGO
