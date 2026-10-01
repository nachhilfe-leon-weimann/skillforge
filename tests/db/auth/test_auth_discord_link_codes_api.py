"""Discord link codes through the real app: an admin issues one, the bot redeems it (bot-decoupling P1-1)."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth.token import GrantType
from app.core.auth import Scope
from app.core.auth.secrets import digest
from app.core.db.models import AuthAuditLog, Party, UserAccount, UserActionToken, UserActionTokenPurpose
from app.services.auth.audit import AuditEventType, AuditSubjectType
from app.services.crm.parties import delete_party

pytestmark = pytest.mark.db

INVALID_ACTION_TOKEN = {"detail": "Invalid or expired token", "code": "invalid_action_token"}
SNOWFLAKE = "123456789012345678"
OTHER = "987654321098765432"
LINK_BOT = "application:00000000-0000-0000-0000-000000000001"


async def _account(client: AsyncClient, party: Party, **body: object) -> str:
    response = await client.post("/users", json={"party_id": str(party.id), **body})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _link_code(client: AsyncClient, user_id: str) -> str:
    response = await client.post(f"/users/{user_id}/discord-link-code")
    assert response.status_code == 201, response.text
    return response.json()["token"]


async def _redeem(link_bot: AsyncClient, code: str, discord_user_id: str = SNOWFLAKE):
    return await link_bot.post("/discord-links/redeem", json={"token": code, "discord_user_id": discord_user_id})


async def _link_events(session: AsyncSession, discord_user_id: str) -> Counter[str]:
    rows = await session.scalars(
        select(AuthAuditLog.event_type).where(
            AuthAuditLog.principal_type == AuditSubjectType.DISCORD_USER, AuthAuditLog.principal_id == discord_user_id
        )
    )
    return Counter(rows)


# --- Issue ---------------------------------------------------------------------------------------


async def test_issuing_answers_the_code_once_and_stores_only_its_digest(
    operator: AsyncClient, make_person, session: AsyncSession, auth_settings, audit_events
):
    user_id = await _account(operator, await make_person())
    before = datetime.now(UTC)

    response = await operator.post(f"/users/{user_id}/discord-link-code")

    body = response.json()
    assert (response.status_code, set(body)) == (201, {"token", "expires_at"})
    assert body["token"].startswith("sf_ua_")
    lifetime = timedelta(hours=auth_settings.discord_link_code_expire_hours)
    assert timedelta(0) <= datetime.fromisoformat(body["expires_at"]) - before - lifetime < timedelta(minutes=1)
    stored = await session.scalar(select(UserActionToken).where(UserActionToken.user_account_id == UUID(user_id)))
    assert stored is not None
    assert (stored.purpose, stored.token_hash) == (UserActionTokenPurpose.DISCORD_LINK, digest(body["token"]))
    assert (await audit_events(UUID(user_id)))[AuditEventType.DISCORD_LINK_CODE_ISSUED] == 1


async def test_a_new_code_invalidates_the_unused_one_and_leaves_other_purposes_alone(
    operator: AsyncClient, make_person, live_tokens
):
    user_id = await _account(operator, await make_person(), email="anna@example.org")
    invitation = (await operator.post(f"/users/{user_id}/invitation")).json()["token"]
    first = await _link_code(operator, user_id)
    second = await _link_code(operator, user_id)

    live = {token.token_hash for token in await live_tokens(UUID(user_id))}

    assert live == {digest(invitation), digest(second)}
    assert digest(first) not in live


async def test_a_disabled_account_gets_no_code(operator: AsyncClient, make_person, live_tokens):
    user_id = await _account(operator, await make_person())
    await operator.patch(f"/users/{user_id}", json={"status": "disabled"})

    response = await operator.post(f"/users/{user_id}/discord-link-code")

    assert (response.status_code, response.json()["code"]) == (409, "user_account_state")
    assert await live_tokens(UUID(user_id)) == []


async def test_an_unknown_account_gets_no_code(operator: AsyncClient):
    response = await operator.post(f"/users/{uuid4()}/discord-link-code")

    assert (response.status_code, response.json()["code"]) == (404, "user_account_not_found")


# --- Purposes stay apart -------------------------------------------------------------------------


async def test_a_link_code_sets_no_password_and_stays_live(
    operator: AsyncClient, make_person, session: AsyncSession, password, live_tokens
):
    user_id = await _account(operator, await make_person(), email="anna@example.org")
    code = await _link_code(operator, user_id)

    response = await operator.post("/password/redeem", json={"token": code, "new_password": password})

    account = await session.scalar(
        select(UserAccount).where(UserAccount.id == UUID(user_id)).execution_options(populate_existing=True)
    )
    assert (response.status_code, response.json()) == (422, INVALID_ACTION_TOKEN)
    assert account is not None and account.password_hash is None
    assert [token.token_hash for token in await live_tokens(UUID(user_id))] == [digest(code)]


async def test_changing_the_email_leaves_a_link_code_live(operator: AsyncClient, make_person, live_tokens):
    user_id = await _account(operator, await make_person(), email="anna@example.org")
    await operator.post(f"/users/{user_id}/invitation")
    code = await _link_code(operator, user_id)

    response = await operator.patch(f"/users/{user_id}", json={"email": "new@example.org"})

    assert response.status_code == 200
    assert [token.token_hash for token in await live_tokens(UUID(user_id))] == [digest(code)]


# --- Redeem --------------------------------------------------------------------------------------


async def test_redeeming_links_the_codes_party_and_spends_the_code(
    link_admin: AsyncClient, link_bot: AsyncClient, make_person, session: AsyncSession, audit_events, live_tokens
):
    person = await make_person()
    user_id = await _account(link_admin, person)
    code = await _link_code(link_admin, user_id)

    redeemed = await _redeem(link_bot, code)
    again = await _redeem(link_bot, code, OTHER)

    assert redeemed.status_code == 200
    assert {key: redeemed.json()[key] for key in ("discord_user_id", "party_id", "active")} == {
        "discord_user_id": SNOWFLAKE,
        "party_id": str(person.id),
        "active": True,
    }
    assert (again.status_code, again.json()) == (422, INVALID_ACTION_TOKEN)
    assert await live_tokens(UUID(user_id)) == []
    assert (await link_admin.get(f"/discord-links/{OTHER}")).status_code == 404
    assert (await audit_events(UUID(user_id)))[AuditEventType.DISCORD_LINK_CODE_REDEEMED] == 1
    assert await _link_events(session, SNOWFLAKE) == Counter({AuditEventType.DISCORD_LINK_ADDED: 1})
    detail = await session.scalar(
        select(AuthAuditLog.detail).where(AuthAuditLog.event_type == AuditEventType.DISCORD_LINK_CODE_REDEEMED)
    )
    assert detail == f"Redeemed a Discord link code for Discord user {SNOWFLAKE} by {LINK_BOT}."


async def test_a_code_moves_an_unlinked_discord_user_to_its_party(
    link_admin: AsyncClient, link_bot: AsyncClient, make_person, session: AsyncSession
):
    first, second = await make_person("Anna"), await make_person("Ben")
    await link_admin.put(f"/discord-links/{SNOWFLAKE}", json={"party_id": str(first.id)})
    await link_admin.delete(f"/discord-links/{SNOWFLAKE}")
    code = await _link_code(link_admin, await _account(link_admin, second))

    response = await _redeem(link_bot, code)

    assert (response.status_code, response.json()["party_id"], response.json()["active"]) == (200, str(second.id), True)
    assert (await _link_events(session, SNOWFLAKE))[AuditEventType.DISCORD_LINK_MOVED] == 1


async def test_a_discord_user_linked_elsewhere_is_409_and_the_code_stays_live(
    link_admin: AsyncClient, link_bot: AsyncClient, make_person, live_tokens
):
    first, second = await make_person("Anna"), await make_person("Ben")
    await link_admin.put(f"/discord-links/{SNOWFLAKE}", json={"party_id": str(first.id)})
    user_id = await _account(link_admin, second)
    code = await _link_code(link_admin, user_id)

    refused = await _redeem(link_bot, code)
    live = [token.token_hash for token in await live_tokens(UUID(user_id))]
    redeemed = await _redeem(link_bot, code, OTHER)

    assert (refused.status_code, refused.json()["code"]) == (409, "discord_account_already_linked")
    assert live == [digest(code)]
    assert (redeemed.status_code, redeemed.json()["party_id"]) == (200, str(second.id))
    assert (await link_admin.get(f"/discord-links/{SNOWFLAKE}")).json()["party_id"] == str(first.id)


async def test_every_refused_code_answers_the_same_body_and_links_nothing(
    link_admin: AsyncClient, link_bot: AsyncClient, make_person, session: AsyncSession
):
    used = await _link_code(link_admin, await _account(link_admin, await make_person("Anna")))
    assert (await _redeem(link_bot, used, OTHER)).status_code == 200
    expired = await _link_code(link_admin, await _account(link_admin, await make_person("Ben")))
    token = await session.scalar(select(UserActionToken).where(UserActionToken.token_hash == digest(expired)))
    assert token is not None
    token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.flush()
    replaced_user = await _account(link_admin, await make_person("Carla"))
    replaced = await _link_code(link_admin, replaced_user)
    await _link_code(link_admin, replaced_user)
    invited_user = await _account(link_admin, await make_person("Dora"), email="dora@example.org")
    invitation = (await link_admin.post(f"/users/{invited_user}/invitation")).json()["token"]
    disabled_user = await _account(link_admin, await make_person("Emil"))
    disabled = await _link_code(link_admin, disabled_user)
    await link_admin.patch(f"/users/{disabled_user}", json={"status": "disabled"})
    leaving = await make_person("Fritz")
    gone = await _link_code(link_admin, await _account(link_admin, leaving))
    await delete_party(session, leaving.id)

    codes = [used, expired, replaced, "sf_ua_unknown", invitation, disabled, gone]
    answers = [await _redeem(link_bot, code) for code in codes]

    assert [(answer.status_code, answer.json()) for answer in answers] == [(422, INVALID_ACTION_TOKEN)] * len(codes)
    assert (await link_admin.get(f"/discord-links/{SNOWFLAKE}")).status_code == 404


async def test_a_redeemed_code_lets_the_bot_exchange_the_discord_user(
    link_admin: AsyncClient, link_bot: AsyncClient, token_api: AsyncClient, make_person, make_login_client
):
    """The arc end to end: the link a code wrote is the identity the token exchange of P0-7 looks up."""
    code = await _link_code(link_admin, await _account(link_admin, await make_person()))
    assert (await _redeem(link_bot, code)).status_code == 200
    bot = await make_login_client(application=(Scope.AUTH_USERS_EXCHANGE,), delegated=(Scope.CRM_READ,))

    response = await token_api.post(
        "/token", auth=bot.basic, data={"grant_type": GrantType.DISCORD_USER.value, "discord_user_id": SNOWFLAKE}
    )

    assert response.status_code == 200, response.text
    assert response.json()["scope"] == "crm:read:own"
