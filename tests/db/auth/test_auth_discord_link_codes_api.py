"""Discord link codes through the real app: an admin issues one, the bot redeems it (bot-decoupling P1-1)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.secrets import digest
from app.core.db.models import Party, UserAccount, UserActionToken, UserActionTokenPurpose
from app.services.auth.audit import AuditEventType

pytestmark = pytest.mark.db

INVALID_ACTION_TOKEN = {"detail": "Invalid or expired token", "code": "invalid_action_token"}


async def _account(client: AsyncClient, party: Party, **body: object) -> str:
    response = await client.post("/users", json={"party_id": str(party.id), **body})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _link_code(client: AsyncClient, user_id: str) -> str:
    response = await client.post(f"/users/{user_id}/discord-link-code")
    assert response.status_code == 201, response.text
    return response.json()["token"]


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
