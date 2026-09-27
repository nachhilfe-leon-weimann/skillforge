"""The Discord link routes against the database: the round trip, the feed, the audit (bot-decoupling P0-2)."""

from collections import Counter

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import AuthAuditLog
from app.services.auth.audit import AuditSubjectType

pytestmark = pytest.mark.db

SNOWFLAKE = "123456789012345678"
OTHER = "987654321098765432"


async def test_link_unlink_read_and_link_again(link_admin: AsyncClient, session: AsyncSession, make_person):
    person = await make_person()

    linked = await link_admin.put(f"/discord-links/{SNOWFLAKE}", json={"party_id": str(person.id)})
    unlinked = await link_admin.delete(f"/discord-links/{SNOWFLAKE}")
    read = await link_admin.get(f"/discord-links/{SNOWFLAKE}")
    again = await link_admin.put(f"/discord-links/{SNOWFLAKE}", json={"party_id": str(person.id)})

    assert (linked.status_code, linked.json()["discord_user_id"], linked.json()["active"]) == (200, SNOWFLAKE, True)
    assert unlinked.status_code == 204
    assert (read.status_code, read.json()["active"]) == (200, False)
    assert (again.status_code, again.json()["active"]) == (200, True)
    rows = (
        await session.execute(
            select(AuthAuditLog.event_type, AuthAuditLog.detail).where(
                AuthAuditLog.principal_type == AuditSubjectType.DISCORD_USER, AuthAuditLog.principal_id == SNOWFLAKE
            )
        )
    ).all()
    assert Counter(event for event, _ in rows) == Counter({
        "discord_link.added": 1,
        "discord_link.removed": 1,
        "discord_link.reactivated": 1,
    })
    assert all(detail.endswith("by application:00000000-0000-0000-0000-000000000001.") for _, detail in rows)


async def test_a_repeated_put_answers_the_same_body(link_admin: AsyncClient, make_person):
    person = await make_person()

    first = await link_admin.put(f"/discord-links/{SNOWFLAKE}", json={"party_id": str(person.id)})
    second = await link_admin.put(f"/discord-links/{SNOWFLAKE}", json={"party_id": str(person.id)})

    assert first.json() == second.json()


async def test_a_company_is_refused_with_422(link_admin: AsyncClient, make_company):
    company = await make_company()

    response = await link_admin.put(f"/discord-links/{SNOWFLAKE}", json={"party_id": str(company.id)})

    assert (response.status_code, response.json()["code"]) == (422, "link_party_not_a_person")


async def test_the_feed_pages_by_discord_id_and_round_trips_its_cursor(link_admin: AsyncClient, make_person):
    person = await make_person()
    for discord_user_id in (OTHER, SNOWFLAKE):
        await link_admin.put(f"/discord-links/{discord_user_id}", json={"party_id": str(person.id)})

    page = (await link_admin.get("/discord-links", params={"party_id": str(person.id), "limit": 1})).json()
    newest = page["items"][0]["updated_at"]
    feed_params = {"party_id": str(person.id), "updated_since": newest}
    again = (await link_admin.get("/discord-links", params=feed_params)).json()

    assert (page["total"], page["items"][0]["discord_user_id"]) == (2, SNOWFLAKE)
    assert SNOWFLAKE in {item["discord_user_id"] for item in again["items"]}
